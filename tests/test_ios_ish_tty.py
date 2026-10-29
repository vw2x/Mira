"""Exercise production iSH TTY input/read with real host condition variables."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party/ish/fs/tty.c"
HEADER = ROOT / "third_party/ish/fs/tty.h"


PREAMBLE = r"""
#include <assert.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>

typedef uint32_t dword_t;
typedef uint8_t byte_t;
typedef uint16_t word_t;
typedef int pid_t_;
typedef uint64_t sigset_t_;
typedef pthread_mutex_t lock_t;
typedef pthread_cond_t cond_t;
"""


STUBS = r"""
#define _EAGAIN (-11)
#define _EINTR (-4)
#define _EIO (-5)
#define _ETIMEDOUT (-110)
#define POLL_READ 1
#define SIGINT_ 2
#define SIGQUIT_ 3
#define SIGTSTP_ 20
#define SIGTTIN_ 21
#define NUM_SIGS 64
#define SIGINFO_NIL NULL
#define O_NONBLOCK_ 04000

struct tty;
struct tty_driver_ops {
    int (*write)(struct tty *, const void *, size_t, bool);
};
struct tty_driver { const struct tty_driver_ops *ops; };
struct tty {
    struct tty_driver *driver;
    char buf[TTY_BUF_SIZE];
    bool buf_flag[TTY_BUF_SIZE];
    size_t bufsize;
    cond_t produced, consumed;
    lock_t lock;
    struct termios_ termios;
    pid_t_ fg_group;
    bool hung_up;
    uint8_t packet_flags;
    struct { bool packet_mode; } pty;
};
struct fd { struct tty *tty; int flags; };
static struct tty_driver driver, pty_master;
static struct tty terminal = {
    .driver = &driver,
    .lock = PTHREAD_MUTEX_INITIALIZER,
    .produced = PTHREAD_COND_INITIALIZER,
    .consumed = PTHREAD_COND_INITIALIZER,
};
static lock_t pids_lock = PTHREAD_MUTEX_INITIALIZER;
static struct group { pid_t_ pgid; } group;
static struct task { struct group *group; } task = { &group };
static struct task *current = &task;
static pthread_mutex_t ready_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready_cond = PTHREAD_COND_INITIALIZER;
static bool reader_waiting;

static void lock(lock_t *mutex) { assert(!pthread_mutex_lock(mutex)); }
static void unlock(lock_t *mutex) { assert(!pthread_mutex_unlock(mutex)); }
static void notify(cond_t *cond) { assert(!pthread_cond_broadcast(cond)); }
static int wait_for(cond_t *cond, lock_t *mutex, struct timespec *timeout) {
    assert(timeout == NULL); // These cases use VMIN=1, VTIME=0.
    if (cond == &terminal.produced) {
        lock(&ready_lock);
        reader_waiting = true;
        notify(&ready_cond);
        unlock(&ready_lock);
    }
    // Matches util/sync.c's wait without guest task signal delivery.
    assert(!pthread_cond_wait(cond, mutex));
    return 0;
}

// Job control, poll subscribers, and driver echo are outside these raw-mode cases.
static void tty_poll_wakeup(struct tty *tty, int events) { (void)tty; (void)events; }
static bool pty_is_half_closed_master(struct tty *tty) { (void)tty; return false; }
static int tty_signal_if_background(struct tty *tty, pid_t_ pgid, int sig) {
    (void)tty; (void)pgid; (void)sig; return 0;
}
static void sigset_add(sigset_t_ *set, int sig) { *set |= UINT64_C(1) << sig; }
static bool sigset_has(sigset_t_ set, int sig) { return set & (UINT64_C(1) << sig); }
static void send_group_signal(pid_t_ pgid, int sig, const void *info) {
    (void)pgid; (void)sig; (void)info;
}
"""


CASES = r"""
#define REQUIRE(condition, message) do { \
    if (!(condition)) { fprintf(stderr, "%s\n", message); return 1; } \
} while (0)

static char input[TTY_BUF_SIZE * 2 + 1], output[sizeof(input)];
static size_t input_size, received;
static struct fd fd = { .tty = &terminal };

static void *reader(void *unused) {
    (void)unused;
    while (received < input_size) {
        size_t amount = input_size - received;
        if (amount > 997) amount = 997;
        ssize_t count = tty_read(&fd, output + received, amount);
        if (count <= 0) return (void *)1;
        received += count;
    }
    return NULL;
}

int main(int argc, char **argv) {
    REQUIRE(argc == 2, "one scenario is required");
    terminal.termios.cc[VMIN_] = 1;
    for (size_t i = 0; i < sizeof(input); i++)
        input[i] = (char)('a' + i % 26);

    if (!strcmp(argv[1], "nonblocking")) {
        ssize_t written = tty_input(&terminal, input, sizeof(input), false);
        REQUIRE(written == TTY_BUF_SIZE, "nonblocking input did not return its partial count");
        REQUIRE(tty_input(&terminal, input + written, 1, false) == _EAGAIN,
                "full nonblocking input did not return EAGAIN");
        REQUIRE(tty_read(&fd, output, sizeof(output)) == written,
                "nonblocking input could not be read back");
        REQUIRE(!memcmp(input, output, written), "nonblocking input changed bytes");
    } else {
        if (!strcmp(argv[1], "capacity")) input_size = TTY_BUF_SIZE;
        else if (!strcmp(argv[1], "eight_kib")) input_size = TTY_BUF_SIZE * 2;
        else if (!strcmp(argv[1], "eight_kib_plus_one")) input_size = sizeof(input);
        else REQUIRE(false, "unknown scenario");

        pthread_t thread;
        REQUIRE(!pthread_create(&thread, NULL, reader, NULL), "reader thread creation failed");
        lock(&ready_lock);
        while (!reader_waiting)
            assert(!pthread_cond_wait(&ready_cond, &ready_lock));
        unlock(&ready_lock);
        // The reader still holds terminal.lock until its produced wait begins;
        // this write cannot race ahead of that wait.
        REQUIRE(tty_input(&terminal, input, input_size, true) == (ssize_t)input_size,
                "blocking input returned before accepting every byte");
        void *reader_error;
        REQUIRE(!pthread_join(thread, &reader_error) && reader_error == NULL,
                "reader failed");
        REQUIRE(received == input_size, "reader did not receive every byte");
        REQUIRE(!memcmp(input, output, input_size), "input was reordered, lost, or duplicated");
    }
    REQUIRE(terminal.bufsize == 0, "read left unexpected bytes in the TTY");
    printf("PASS: %s\n", argv[1]);
    return 0;
}
"""


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}\n", start) + len("\n}\n")]


class IshTtyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang") or shutil.which("cc")
        if compiler is None:
            raise RuntimeError("A host C compiler is required for the iSH TTY regression")
        source = SOURCE.read_text(encoding="utf-8")
        header = HEADER.read_text(encoding="utf-8")
        termios = header[header.index("struct termios_ {"):header.index("struct tty_driver {")]
        buffer_size = next(line for line in header.splitlines() if line.startswith("#define TTY_BUF_SIZE "))
        functions = "\n".join(function(source, signature) for signature in (
            "static void tty_input_wakeup(",
            "static int tty_push_char(",
            "static void tty_echo(",
            "static bool tty_send_input_signal(",
            "ssize_t tty_input(",
            "static void tty_read_into_buf(",
            "static size_t tty_canon_size(",
            "static ssize_t tty_read(",
        ))
        cls.temporary = tempfile.TemporaryDirectory(prefix="mira-ish-tty-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        harness = directory / "tty_test.c"
        cls.executable = directory / "tty_test"
        harness.write_text(
            PREAMBLE + termios + buffer_size + "\n" + STUBS + functions + CASES,
            encoding="utf-8",
        )
        result = subprocess.run(
            [compiler, "-std=c11", "-Wall", "-Wextra", "-pthread", str(harness),
             "-o", str(cls.executable)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"iSH TTY harness did not compile:\n{result.stderr}")

    def run_case(self, scenario):
        try:
            result = subprocess.run(
                [str(self.executable), scenario],
                capture_output=True, text=True, timeout=5,
            )
        except subprocess.TimeoutExpired as error:
            raise AssertionError(
                f"{scenario}: TTY input/read did not complete within 5 seconds"
            ) from error
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"PASS: {scenario}", result.stdout)

    def test_one_buffer_wakes_a_waiting_reader(self):
        self.run_case("capacity")

    def test_eight_kib_reaches_a_waiting_reader(self):
        self.run_case("eight_kib")

    def test_more_than_two_buffers_reaches_a_waiting_reader(self):
        self.run_case("eight_kib_plus_one")

    def test_nonblocking_input_reports_partial_write_and_eagain(self):
        self.run_case("nonblocking")


if __name__ == "__main__":
    unittest.main()
