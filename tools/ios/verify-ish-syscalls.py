"""Run with guest Python 3.11 inside Mira's iSH i386 runtime; never on the host."""
import ctypes
import errno
import json
import os
import struct
import sys


# Pinned iSH kernel/calls.c: munmap=91, mmap2=192, getdents64=220,
# timer_create=259, timer_delete=263. timer_gettime is not implemented.
SYS_MUNMAP = 91
SYS_MMAP2 = 192
SYS_GETDENTS64 = 220
SYS_TIMER_CREATE = 259
SYS_TIMER_DELETE = 263
PAGE_SIZE = 4096  # emu/mmu.h: PAGE_BITS=12, independent of host page size.
ANONYMOUS_PRIVATE = 0x22  # kernel/calls.h


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)


def raw(number, *args):
    ctypes.set_errno(0)
    values = [ctypes.c_long(value) if isinstance(value, int) else value for value in args]
    result = libc.syscall(ctypes.c_long(number), *values)
    return result, ctypes.get_errno()


def checked(number, *args):
    result, error = raw(number, *args)
    if result == -1:
        raise OSError(error, "iSH syscall %d failed" % number)
    return result


def mmap_hint():
    allocations = set()
    try:
        first = checked(SYS_MMAP2, 0, PAGE_SIZE, 3, ANONYMOUS_PRIVATE, -1, 0) & 0xffffffff
        require(first != 0, "first mmap returned NULL")
        allocations.add(first)
        ctypes.c_ubyte.from_address(first).value = 0x5a
        second = checked(SYS_MMAP2, first, PAGE_SIZE, 3, ANONYMOUS_PRIVATE, -1, 0) & 0xffffffff
        require(second != 0, "second mmap returned NULL")
        allocations.add(second)
        preserved = ctypes.c_ubyte.from_address(first).value == 0x5a
        require(second != first and preserved, "occupied mmap hint replaced the original mapping")
        require(ctypes.c_ubyte.from_address(second).value == 0, "anonymous mapping was not zeroed")
        return {"distinct_mapping": True, "sentinel_preserved": preserved}
    finally:
        for address in allocations:
            checked(SYS_MUNMAP, address, PAGE_SIZE)


def dirent_names(data):
    # fs/dir.c uses packed linux_dirent64_: u64 ino, u64 off, u16 reclen,
    # u8 type, then the NUL-terminated name. It does not round records to 8 bytes.
    names = []
    offset = 0
    while offset < len(data):
        require(len(data) - offset >= 20, "short getdents64 record")
        length = struct.unpack_from("<H", data, offset + 16)[0]
        require(length >= 20 and offset + length <= len(data), "invalid getdents64 record length")
        name = data[offset + 19:offset + length]
        require(b"\0" in name, "unterminated getdents64 name")
        names.append(name.split(b"\0", 1)[0])
        offset += length
    return names


def getdents_small_buffer():
    results = []
    for path in ("/", "/mira/proc/self"):
        first = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        reference = None
        try:
            reference = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            buffer = ctypes.create_string_buffer(4096)
            result, error = raw(SYS_GETDENTS64, first, ctypes.byref(buffer), 1)
            require(result == -1 and error == errno.EINVAL, "tiny getdents64 buffer did not return EINVAL")
            size = checked(SYS_GETDENTS64, first, ctypes.byref(buffer), len(buffer))
            names = dirent_names(buffer.raw[:size])
            size = checked(SYS_GETDENTS64, reference, ctypes.byref(buffer), len(buffer))
            expected = dirent_names(buffer.raw[:size])
            require(bool(names) and names == expected, "tiny getdents64 call lost directory position")
            results.append({"path": path, "errno": error, "entries_after_retry": len(names)})
        finally:
            os.close(first)
            if reference is not None:
                os.close(reference)
    return {"directories": results}


def proc_self_mem():
    expected = b"Mira iSH own-memory probe 2026"
    buffer = ctypes.create_string_buffer(expected)
    address = ctypes.addressof(buffer)
    descriptor = os.open("/proc/self/mem", os.O_RDONLY)
    try:
        # lseek exercises proc_refresh_data -> the newly added .show callback.
        require(os.lseek(descriptor, address, os.SEEK_SET) == address, "proc mem seek mismatch")
        actual = os.read(descriptor, len(expected))
        require(actual == expected, "proc mem did not return this process's known buffer")
        return {"seek_and_read": True, "bytes": len(actual)}
    finally:
        os.close(descriptor)


class Sigevent(ctypes.Structure):
    # kernel/signal.h sigevent_: sigval (4 bytes), signo, method, tid.
    _fields_ = [("value", ctypes.c_uint32), ("signo", ctypes.c_int32),
                ("method", ctypes.c_int32), ("tid", ctypes.c_int32)]


def create_timer():
    event = Sigevent(0, 0, 1, 0)  # CLOCK_REALTIME=0, SIGEV_NONE=1; never armed.
    timer = ctypes.c_uint32()
    checked(SYS_TIMER_CREATE, 0, ctypes.byref(event), ctypes.byref(timer))
    return timer.value


def fork_timer():
    read_fd, write_fd = os.pipe()
    parent_timer = None
    child_pid = None
    try:
        parent_timer = create_timer()
        sys.stdout.flush()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            child_timer = None
            payload = {"error": "timer child did not complete"}
            try:
                # time.c allocates the first unused slot. fork.c must clear all
                # slots, so the child's first timer is 0 despite a parent timer.
                # Never delete an inherited ID: old iSH aliases its native pointer.
                child_timer = create_timer()
                created_timer_id = child_timer
                checked(SYS_TIMER_DELETE, child_timer)
                child_timer = None
                payload = {"child_first_timer": created_timer_id}
            except Exception as error:
                payload = {"error": str(error)}
            finally:
                try:
                    if child_timer is not None:
                        raw(SYS_TIMER_DELETE, child_timer)
                    os.write(write_fd, json.dumps(payload).encode())
                finally:
                    # Never enter the parent's cleanup with its inherited ID.
                    os._exit(0 if "error" not in payload else 1)
        os.close(write_fd)
        write_fd = -1
        pieces = []
        while True:
            piece = os.read(read_fd, 4096)
            if not piece:
                break
            pieces.append(piece)
        _, status = os.waitpid(child_pid, 0)
        child_pid = None
        payload = json.loads(b"".join(pieces))
        require(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, "timer child failed: %r" % payload)
        require(payload.get("child_first_timer") == 0, "child inherited occupied POSIX timer slots")
        checked(SYS_TIMER_DELETE, parent_timer)
        parent_timer = None
        return {"child_first_timer": payload["child_first_timer"], "parent_timer_survived": True}
    finally:
        if child_pid not in (None, 0):
            os.waitpid(child_pid, 0)
        os.close(read_fd)
        if write_fd != -1:
            os.close(write_fd)
        if parent_timer is not None:
            checked(SYS_TIMER_DELETE, parent_timer)


def main():
    global libc
    require(sys.platform == "linux" and ctypes.sizeof(ctypes.c_void_p) == 4,
            "this probe requires 32-bit Linux iSH guest Python")
    require(os.uname().machine == "i686" and "ish" in os.uname().release,
            "this probe requires iSH, not a native Linux process")
    require(ctypes.sizeof(Sigevent) == 16, "unexpected sigevent layout")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    probes = (("mmap_hint", mmap_hint), ("getdents64", getdents_small_buffer),
              ("proc_self_mem", proc_self_mem), ("fork_posix_timer", fork_timer))
    passed = 0
    for name, probe in probes:
        emit({"probe": name, "phase": "start"})
        try:
            evidence = probe()
            passed += 1
            emit({"probe": name, "ok": True, "evidence": evidence})
        except Exception as error:
            emit({"probe": name, "ok": False, "error": str(error)})
    emit({"ok": passed == len(probes), "passed": passed, "total": len(probes)})
    return 0 if passed == len(probes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
