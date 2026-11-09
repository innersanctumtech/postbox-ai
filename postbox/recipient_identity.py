"""Resolve a local session recipient from a trusted connected socket."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import uuid


class IdentityUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RecipientIdentity:
    pid: int
    session_id: str
    process_start: str
    socket_path: str
    socket_device: int
    socket_inode: int


def local_lookups(session_directory, socket_directory, *, session_app_names):
    sessions, sockets = Path(session_directory), Path(socket_directory)
    if (not sessions.is_absolute() or not sockets.is_absolute()
            or not isinstance(session_app_names, (set, frozenset, tuple, list)) or not session_app_names
            or any(not isinstance(name, str) or not name or os.path.basename(name) != name for name in session_app_names)):
        raise IdentityUnavailable("absolute identity directories required")
    app_names = frozenset(session_app_names)

    def trusted_dir(path):
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise IdentityUnavailable("identity directory unavailable")

    def process_lookup(pid):
        if isinstance(pid, bool) or not isinstance(pid, int) or not 1 < pid <= 2**31 - 1:
            raise IdentityUnavailable("invalid process PID")
        env = dict(os.environ, LC_ALL="C")
        try:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "uid=", "-o", "lstart=", "-o", "comm="],
                                    capture_output=True, text=True, timeout=3, env=env, check=True)
            values = result.stdout.strip().split(None, 8)
            if len(values) != 9:
                raise ValueError("invalid process snapshot")
            actual, parent, owner = map(int, values[:3])
            started, executable = " ".join(values[3:8]), values[8]
            if actual != pid or owner != os.getuid() or not os.path.isabs(executable):
                raise ValueError("process identity mismatch")
            time.strptime(started, "%a %b %d %H:%M:%S %Y")
            return {"pid": pid, "ppid": parent, "start": started,
                    "is_session_app": os.path.basename(executable) in app_names}
        except Exception as exc:
            raise IdentityUnavailable("process evidence unavailable") from exc

    def session_lookup(pid):
        try:
            trusted_dir(sessions)
            path = sessions / f"{pid}.json"
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info, named = os.fstat(fd), os.lstat(path)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                        or info.st_size > 65536 or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
                    raise ValueError("unsafe session record")
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    data = json.load(handle)
            finally:
                os.close(fd)
            process = process_lookup(pid)
            began = data.get("startedAt") if isinstance(data, dict) else None
            if (not isinstance(data, dict) or data.get("pid") != pid or isinstance(began, bool)
                    or not isinstance(began, (int, float)) or not 1e12 < began < 1e14
                    or abs(time.mktime(time.strptime(process["start"], "%a %b %d %H:%M:%S %Y")) - began / 1000) > 2):
                raise ValueError("session incarnation mismatch")
            return {"pid": pid, "session_id": data.get("sessionId"), "process_start": process["start"],
                    "socket_path": str(sockets / f"{pid}.sock")}
        except Exception as exc:
            raise IdentityUnavailable("session evidence unavailable") from exc

    def socket_lookup(path):
        try:
            trusted_dir(sockets)
            if Path(path).parent != sockets:
                raise ValueError("unexpected socket location")
            return os.lstat(path)
        except Exception as exc:
            raise IdentityUnavailable("socket evidence unavailable") from exc

    return process_lookup, session_lookup, socket_lookup


def _peer_pid(connection):
    if sys.platform != "darwin" or getattr(connection, "family", None) != socket.AF_UNIX:
        raise IdentityUnavailable("local peer identity unavailable")
    try:
        peer = connection.getsockopt(0, 0x002)
    except (OSError, AttributeError, TypeError) as exc:
        raise IdentityUnavailable("cannot obtain local peer PID") from exc
    if isinstance(peer, bool) or not isinstance(peer, int) or not 1 < peer <= 2**31 - 1:
        raise IdentityUnavailable("invalid peer PID")
    return peer


def connect_verified(identity, *, process_lookup, session_lookup, socket_lookup, timeout=3.0):
    import math
    if (not isinstance(identity, RecipientIdentity) or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 5):
        raise IdentityUnavailable("verified identity and bounded timeout required")

    def snapshot():
        process, session = process_lookup(identity.pid), session_lookup(identity.pid)
        info = socket_lookup(identity.socket_path)
        if (not isinstance(process, dict) or process.get("pid") != identity.pid
                or process.get("is_session_app") is not True or process.get("start") != identity.process_start
                or not isinstance(session, dict) or session.get("pid") != identity.pid
                or session.get("session_id") != identity.session_id
                or session.get("process_start") != identity.process_start
                or session.get("socket_path") != identity.socket_path or not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid() or (info.st_dev, info.st_ino) != (identity.socket_device, identity.socket_inode)):
            raise IdentityUnavailable("session or socket identity changed")

    connection = None
    try:
        snapshot()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(identity.socket_path)
        if _peer_pid(connection) != identity.pid:
            raise IdentityUnavailable("socket server PID mismatch")
        snapshot()
        if _peer_pid(connection) != identity.pid:
            raise IdentityUnavailable("socket server PID changed")
        return connection
    except Exception as exc:
        if connection is not None:
            connection.close()
        if isinstance(exc, IdentityUnavailable):
            raise
        raise IdentityUnavailable("verified connection unavailable") from exc


def identify_peer(connection, *, process_lookup, session_lookup, socket_lookup):
    peer = _peer_pid(connection)

    def capture():
        lineage, visited, pid = [], set(), peer
        try:
            for _ in range(64):
                if pid <= 1:
                    break
                if pid in visited:
                    raise IdentityUnavailable("ambiguous process lineage")
                visited.add(pid)
                row = process_lookup(pid)
                if not isinstance(row, dict) or row.get("pid") != pid:
                    raise IdentityUnavailable("process evidence unavailable")
                ppid, started, is_app = row.get("ppid"), row.get("start"), row.get("is_session_app")
                if (isinstance(ppid, bool) or not isinstance(ppid, int) or ppid < 0
                        or not isinstance(started, str) or not started.strip() or not isinstance(is_app, bool)):
                    raise IdentityUnavailable("invalid process evidence")
                lineage.append((pid, ppid, started, is_app))
                pid = ppid
            else:
                raise IdentityUnavailable("process lineage too deep")
            matches = [row for row in lineage if row[3]]
            if len(matches) != 1:
                raise IdentityUnavailable("session lineage is ambiguous")
            session_pid, _, started, _ = matches[0]
            session = session_lookup(session_pid)
            if not isinstance(session, dict) or session.get("pid") != session_pid or session.get("process_start") != started:
                raise IdentityUnavailable("session incarnation changed")
            session_id = session.get("session_id")
            try:
                if not isinstance(session_id, str) or str(uuid.UUID(session_id)) != session_id:
                    raise ValueError("invalid session ID")
            except (ValueError, AttributeError) as exc:
                raise IdentityUnavailable("full session ID unavailable") from exc
            path = session.get("socket_path")
            if not isinstance(path, str) or not os.path.isabs(path) or len(path) > 1024:
                raise IdentityUnavailable("session socket unavailable")
            info = socket_lookup(path)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_dev < 0 or info.st_ino <= 0:
                raise IdentityUnavailable("invalid session socket")
            return tuple(lineage), RecipientIdentity(session_pid, session_id, started, path, info.st_dev, info.st_ino)
        except IdentityUnavailable:
            raise
        except Exception as exc:
            raise IdentityUnavailable("identity evidence unavailable") from exc

    before, after = capture(), capture()
    if before != after or _peer_pid(connection) != peer:
        raise IdentityUnavailable("peer or session identity changed")
    return before[1]
