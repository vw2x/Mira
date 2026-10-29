"""Exercise iSH's production mmap address selection with a small host page table."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party/ish/kernel/mmap.c"


STUBS = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint32_t addr_t, dword_t, page_t, pages_t;
typedef int fd_t;
#define PAGE_BITS 12
#define PAGE_SIZE (1U << PAGE_BITS)
#define PAGE(addr) ((addr) >> PAGE_BITS)
#define PGOFFSET(addr) ((addr) & (PAGE_SIZE - 1))
#define PAGE_ROUND_UP(bytes) PAGE((bytes) + PAGE_SIZE - 1)
#define BAD_PAGE 0x10000
#define MMAP_SHARED 0x1
#define MMAP_PRIVATE 0x2
#define MMAP_FIXED 0x10
#define MMAP_ANONYMOUS 0x20
#define P_SHARED (1 << 7)
#define _EINVAL (-22)
#define _ENOMEM (-12)
#define _EBADF (-9)
#define _ENODEV (-19)
#define PAGE_COUNT 16

struct fd;
struct data { struct fd *fd; dword_t file_offset; unsigned char marker; };
struct pt_entry { struct data *data; };
struct mem {
    bool mapped[PAGE_COUNT];
    struct data data[PAGE_COUNT];
    struct pt_entry entries[PAGE_COUNT];
};
struct fd_ops {
    int (*mmap)(struct fd *, struct mem *, page_t, pages_t,
                dword_t, dword_t, dword_t);
};
struct fd { const struct fd_ops *ops; };
static struct mem memory;
static struct task { struct mem *mem; } task = { &memory };
static struct task *current = &task;

static bool pt_is_hole(struct mem *mem, page_t first, pages_t count) {
    if (first >= PAGE_COUNT || count > PAGE_COUNT - first) return false;
    for (page_t page = first; page < first + count; page++)
        if (mem->mapped[page]) return false;
    return true;
}

static page_t pt_find_hole(struct mem *mem, pages_t count) {
    for (page_t page = PAGE_COUNT - 1; page > 0; page--)
        if (pt_is_hole(mem, page, count)) return page;
    return BAD_PAGE;
}

// Like kernel/memory.c, mapping replaces an existing page and its contents.
static int pt_map_nothing(struct mem *mem, page_t first, pages_t count,
                          unsigned flags) {
    (void)flags;
    if (first >= PAGE_COUNT || count > PAGE_COUNT - first) return _ENOMEM;
    for (page_t page = first; page < first + count; page++) {
        mem->mapped[page] = true;
        mem->data[page] = (struct data) {0};
        mem->entries[page].data = &mem->data[page];
    }
    return 0;
}

// These tests exercise anonymous mappings; file mappings remain unsupported.
static struct fd *f_get(fd_t fd) { (void)fd; return NULL; }
static struct fd *fd_retain(struct fd *fd) { return fd; }
static struct pt_entry *mem_pt(struct mem *mem, page_t page) {
    return &mem->entries[page];
}
"""


CASES = r"""
#define REQUIRE(condition, message) do { \
    if (!(condition)) { fprintf(stderr, "%s\n", message); return 1; } \
} while (0)

int main(int argc, char **argv) {
    REQUIRE(argc == 2, "one scenario is required");
    const addr_t hint = 4 * PAGE_SIZE;
    const dword_t flags = MMAP_PRIVATE | MMAP_ANONYMOUS;
    const dword_t prot = 3;
    addr_t result;

    if (!strcmp(argv[1], "hint_collision")) {
        pt_map_nothing(&memory, PAGE(hint), 1, prot);
        memory.data[PAGE(hint)].marker = 0x5a;
        result = do_mmap(hint, PAGE_SIZE, prot, flags, -1, 0);
        REQUIRE(result != hint, "occupied non-fixed hint replaced the old mapping");
        REQUIRE(PAGE(result) < PAGE_COUNT && memory.mapped[PAGE(result)],
                "occupied non-fixed hint did not allocate a new mapping");
        REQUIRE(memory.mapped[PAGE(hint)] && memory.data[PAGE(hint)].marker == 0x5a,
                "occupied non-fixed hint lost the original contents");
    } else if (!strcmp(argv[1], "fixed_replacement")) {
        pt_map_nothing(&memory, PAGE(hint), 1, prot);
        memory.data[PAGE(hint)].marker = 0x5a;
        result = do_mmap(hint, PAGE_SIZE, prot, flags | MMAP_FIXED, -1, 0);
        REQUIRE(result == hint && memory.mapped[PAGE(hint)],
                "MAP_FIXED did not replace the requested mapping");
        REQUIRE(memory.data[PAGE(hint)].marker == 0,
                "MAP_FIXED anonymous replacement kept the old contents");
    } else if (!strcmp(argv[1], "null_hint")) {
        result = do_mmap(0, PAGE_SIZE, prot, flags, -1, 0);
        REQUIRE(result != 0 && PAGE(result) < PAGE_COUNT,
                "null hint did not select a valid hole");
        REQUIRE(memory.mapped[PAGE(result)], "null hint did not map the hole");
    } else if (!strcmp(argv[1], "no_space")) {
        pt_map_nothing(&memory, 0, PAGE_COUNT, prot);
        for (page_t page = 0; page < PAGE_COUNT; page++)
            memory.data[page].marker = 0x5a;
        REQUIRE(do_mmap(0, PAGE_SIZE, prot, flags, -1, 0) == (addr_t)_ENOMEM,
                "null hint with no free space did not return ENOMEM");
        REQUIRE(do_mmap(hint, PAGE_SIZE, prot, flags, -1, 0) == (addr_t)_ENOMEM,
                "occupied hint with no free space did not return ENOMEM");
        for (page_t page = 0; page < PAGE_COUNT; page++)
            REQUIRE(memory.mapped[page] && memory.data[page].marker == 0x5a,
                    "failed allocation changed an existing mapping");
    } else {
        REQUIRE(false, "unknown scenario");
    }
    printf("PASS: %s\n", argv[1]);
    return 0;
}
"""


class IshMmapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("clang") or shutil.which("cc")
        if compiler is None:
            raise RuntimeError("A host C compiler is required for the iSH mmap regression")
        source = SOURCE.read_text(encoding="utf-8")
        start = source.index("static addr_t do_mmap(")
        end = source.index("\n}\n", start) + len("\n}\n")
        cls.temporary = tempfile.TemporaryDirectory(prefix="mira-ish-mmap-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        harness = directory / "mmap_test.c"
        cls.executable = directory / "mmap_test"
        harness.write_text(STUBS + source[start:end] + CASES, encoding="utf-8")
        result = subprocess.run(
            [compiler, "-std=c11", "-Wall", "-Wextra", str(harness),
             "-o", str(cls.executable)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError(f"iSH mmap harness did not compile:\n{result.stderr}")

    def run_case(self, scenario):
        result = subprocess.run(
            [str(self.executable), scenario],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"PASS: {scenario}", result.stdout)

    def test_occupied_hint_preserves_existing_mapping(self):
        self.run_case("hint_collision")

    def test_map_fixed_replaces_existing_mapping(self):
        self.run_case("fixed_replacement")

    def test_null_hint_allocates_a_hole(self):
        self.run_case("null_hint")

    def test_no_space_returns_enomem_without_changing_mappings(self):
        self.run_case("no_space")


if __name__ == "__main__":
    unittest.main()
