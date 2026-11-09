"""Read a Darwin process identity through libproc with repeated consistency checks."""
import ctypes
from dataclasses import dataclass
import os
import sys


class ProcBSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid", "pbi_uid",
        "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid", "pbi_svgid", "rfu_1",
    )] + [
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
    ] + [(name, ctypes.c_uint32) for name in ("pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid")] + [
        ("pbi_nice", ctypes.c_int32), ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    executable: str
    start_sec: int
    start_usec: int


def read_process_identity(pid, *, platform=sys.platform, lib=None):
    """Return a stable process identity or raise ValueError when it cannot be established."""
    if platform != "darwin" or type(pid) is not int or not 1 < pid <= 2**31 - 1:
        raise ValueError("unsupported platform or PID")
    try:
        if lib is None:
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            lib.proc_pidinfo.restype = ctypes.c_int
            lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            lib.proc_pidpath.restype = ctypes.c_int

        def read_info():
            info = ProcBSDInfo()
            size = ctypes.sizeof(info)
            if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size:
                raise ValueError("incomplete process record")
            if info.pbi_pid != pid or info.pbi_start_tvsec <= 0 or info.pbi_start_tvusec >= 1_000_000:
                raise ValueError("invalid process record")
            return info.pbi_pid, info.pbi_uid, info.pbi_start_tvsec, info.pbi_start_tvusec

        def executable_path():
            buf = ctypes.create_string_buffer(4096)
            length = lib.proc_pidpath(pid, buf, len(buf))
            if type(length) is not int or length <= 0 or length >= len(buf):
                raise ValueError("invalid process path")
            raw = bytes(buf.raw[:length])
            if b"\0" in raw or buf.raw[length] != 0:
                raise ValueError("incomplete process path")
            path = raw.decode("utf-8", "strict")
            if not os.path.isabs(path):
                raise ValueError("invalid process path")
            return path

        before = read_info()
        path = executable_path()
        middle = read_info()
        later_path = executable_path()
        after = read_info()
        if before != middle or middle != after or path != later_path:
            raise ValueError("process incarnation changed")
        return ProcessIdentity(before[0], before[1], path, before[2], before[3])
    except Exception as exc:
        raise ValueError("process identity unavailable") from exc
