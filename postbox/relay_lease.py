"""Exclusive per-process lock for local relay activation."""
import fcntl
import os
import stat
from pathlib import Path


_ACTIVE_LEASES = set()


def _close_child_leases():
    for lease in list(_ACTIVE_LEASES):
        try:
            lease._close_inherited()
        except OSError:
            pass
    _ACTIVE_LEASES.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_close_child_leases)


class LeaseError(Exception):
    """The lease is unsafe, unavailable, or no longer held."""


class RelayLease:
    def __init__(self, path, fd, identity):
        self.path = path
        self._fd = fd
        self._identity = identity
        self._pid = os.getpid()
        _ACTIVE_LEASES.add(self)

    def _close_inherited(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)

    @classmethod
    def acquire(cls, path):
        path = Path(path)
        if not path.is_absolute() or path.parent.resolve() != path.parent:
            raise LeaseError("absolute lease path without symlinked parent required")
        fd = None
        try:
            parent = os.lstat(path.parent)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise LeaseError("lease directory must be private and owned")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            fd = os.open(path, flags, 0o600)
            info = os.fstat(fd)
            named = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(named.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
                raise LeaseError("lease file must be private and owned")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = cls(path, fd, (info.st_dev, info.st_ino))
            result.assert_held()
            return result
        except Exception as exc:
            if fd is not None:
                os.close(fd)
            if isinstance(exc, LeaseError):
                raise
            raise LeaseError("relay lease unavailable") from exc

    def assert_held(self):
        try:
            if self._fd is None or os.getpid() != self._pid:
                raise LeaseError("relay lease is not held by this process")
            parent = os.lstat(self.path.parent)
            opened = os.fstat(self._fd)
            named = os.lstat(self.path)
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700
                    or not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
                    or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_nlink != 1 or (opened.st_dev, opened.st_ino) != self._identity
                    or (named.st_dev, named.st_ino) != self._identity):
                raise LeaseError("relay lease identity changed")
        except LeaseError:
            raise
        except OSError as exc:
            raise LeaseError("relay lease unavailable") from exc

    def close(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
        _ACTIVE_LEASES.discard(self)
