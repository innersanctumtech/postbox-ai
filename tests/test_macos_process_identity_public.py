#!/usr/bin/env python3
"""Synthetic checks for stable process identity reads."""
import ctypes
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import macos_process_identity as process_identity  # noqa: E402


class Call:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


class FakeLib:
    def __init__(self, pid, path=b"/usr/bin/worker"):
        self.pid = pid
        self.path = path
        self.info_reads = 0
        self.path_reads = 0
        self.proc_pidinfo = Call(self.read_info)
        self.proc_pidpath = Call(self.read_path)

    def read_info(self, pid, flavor, arg, output, size):
        self.info_reads += 1
        info = ctypes.cast(output, ctypes.POINTER(process_identity.ProcBSDInfo)).contents
        info.pbi_pid = self.pid
        info.pbi_uid = 501
        info.pbi_start_tvsec = 123456
        info.pbi_start_tvusec = 789012
        return size

    def read_path(self, pid, output, size):
        self.path_reads += 1
        ctypes.memmove(output, self.path + b"\0", len(self.path) + 1)
        return len(self.path)


def check(label, condition):
    if not condition:
        raise AssertionError(label)


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


check("unsupported platforms fail closed", _raises(lambda: process_identity.read_process_identity(42, platform="linux", lib=object())))
check("invalid pid values fail closed", _raises(lambda: process_identity.read_process_identity(True, platform="darwin", lib=object())))

fake = FakeLib(42)
identity = process_identity.read_process_identity(42, platform="darwin", lib=fake)
check("identity retains pid, uid, executable and microsecond start", identity == process_identity.ProcessIdentity(42, 501, "/usr/bin/worker", 123456, 789012))
check("native data is read repeatedly around executable lookup", fake.info_reads == 3 and fake.path_reads == 2)

changing = FakeLib(42)
def changing_info(pid, flavor, arg, output, size):
    changing.info_reads += 1
    info = ctypes.cast(output, ctypes.POINTER(process_identity.ProcBSDInfo)).contents
    info.pbi_pid = pid
    info.pbi_uid = 501
    info.pbi_start_tvsec = 123456 + (changing.info_reads == 3)
    info.pbi_start_tvusec = 789012
    return size

changing.proc_pidinfo = Call(changing_info)
check("a process incarnation change is refused", _raises(lambda: process_identity.read_process_identity(42, platform="darwin", lib=changing)))
print("4 passed, 0 failed")
