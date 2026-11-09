"""Store short lived message bodies in an owned private directory."""

import base64
import binascii
import fcntl
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time


class SpoolError(Exception):
    pass


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_MAX_BODY = 1024 * 1024


class ConfirmationSpool:
    def __init__(self, directory):
        self.directory = Path(directory)
        info = os.lstat(self.directory)
        self._check_dir_stat(info)
        self._identity = (info.st_dev, info.st_ino)

    @staticmethod
    def _check_dir_stat(info):
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("private owned spool directory required")

    @contextmanager
    def _dir(self):
        try:
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise PermissionError("spool directory unavailable") from exc
        try:
            info = os.fstat(fd)
            self._check_dir_stat(info)
            if (info.st_dev, info.st_ino) != self._identity:
                raise PermissionError("spool directory changed")
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("spool lock timeout")
                    time.sleep(0.01)
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _filename(message_id):
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            raise ValueError("invalid message id")
        return message_id + ".json"

    def create(self, message_id, body, *, expires_at):
        filename = self._filename(message_id)
        if not isinstance(body, bytes) or len(body) > _MAX_BODY:
            raise ValueError("body must be bytes up to 1 MiB")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at) or expires_at <= 0:
            raise ValueError("valid expiry required")
        digest = hashlib.sha256(body).hexdigest()
        data = json.dumps({"id": message_id, "sha256": digest,
                           "body": base64.b64encode(body).decode("ascii"),
                           "expires_at": expires_at}, separators=(",", ":")).encode()
        with self._dir() as directory:
            temp = ".tmp-" + secrets.token_hex(16)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temp, filename, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError as exc:
                    raise SpoolError("message already exists") from exc
                os.fsync(directory)
            finally:
                os.unlink(temp, dir_fd=directory)
        return digest

    def _read(self, directory, filename, message_id, expected_digest, expected_expires_at=None):
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2 * _MAX_BODY):
                    raise SpoolError("spool file is unsafe or too large")
                record = json.load(handle)
            body = base64.b64decode(record["body"], validate=True)
            digest = hashlib.sha256(body).hexdigest()
            if (len(body) > _MAX_BODY or record["id"] != message_id
                    or not hmac.compare_digest(record["sha256"], digest)
                    or not hmac.compare_digest(expected_digest, digest)
                    or (expected_expires_at is not None and (type(record.get("expires_at")) not in (int, float)
                        or record["expires_at"] != expected_expires_at))):
                raise SpoolError("message integrity mismatch")
            return body
        except (OSError, ValueError, TypeError, KeyError, binascii.Error) as exc:
            raise SpoolError("spool file unavailable or invalid") from exc

    def read(self, message_id, expected_digest, *, expected_expires_at=None):
        filename = self._filename(message_id)
        if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
            raise ValueError("expected SHA-256 required")
        with self._dir() as directory:
            return self._read(directory, filename, message_id, expected_digest, expected_expires_at)

    def remove_expired(self, message_id, expected_digest):
        filename = self._filename(message_id)
        if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
            raise ValueError("expected SHA-256 required")
        with self._dir() as directory:
            try:
                before = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return False
            self._read(directory, filename, message_id, expected_digest)
            current = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise SpoolError("spool file changed")
            os.unlink(filename, dir_fd=directory)
            os.fsync(directory)
            return True

    def sweep_crash_temps(self, now):
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("trusted time required")
        removed = failures = 0
        with self._dir() as directory:
            for name in os.listdir(directory):
                try:
                    if not re.fullmatch(r"\.tmp-[0-9a-f]{32}", name):
                        continue
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                            raise SpoolError("unsafe temporary file")
                        expiry = None
                        if info.st_size <= 2 * _MAX_BODY:
                            try:
                                with os.fdopen(fd, "rb", closefd=False) as handle:
                                    record = json.load(handle)
                                body = base64.b64decode(record["body"], validate=True)
                                expiry = record["expires_at"]
                                if (not isinstance(record["id"], str) or not _ID.fullmatch(record["id"])
                                        or not isinstance(expiry, (int, float)) or isinstance(expiry, bool)
                                        or not math.isfinite(expiry)
                                        or not hmac.compare_digest(record["sha256"], hashlib.sha256(body).hexdigest())):
                                    expiry = None
                            except (ValueError, TypeError, KeyError, OverflowError, binascii.Error):
                                expiry = None
                        expired = (now >= min(expiry, info.st_mtime + 3600) if expiry is not None
                                   else now - info.st_mtime >= 3600)
                        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if expired and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                            os.unlink(name, dir_fd=directory)
                            removed += 1
                    finally:
                        os.close(fd)
                except (OSError, SpoolError, OverflowError):
                    failures += 1
            if removed:
                os.fsync(directory)
        if failures:
            raise SpoolError(f"{failures} entries could not be cleaned")
        return removed

    def sweep_orphan_bodies(self, now, known_ids):
        if (isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now)
                or not isinstance(known_ids, (set, frozenset))
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in known_ids)):
            raise ValueError("trusted time and ledger identities required")
        removed = failures = 0
        with self._dir() as directory:
            for name in os.listdir(directory):
                try:
                    if not name.endswith(".json") or not _ID.fullmatch(name[:-5]) or name[:-5] in known_ids:
                        continue
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        info = os.fstat(fd)
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 2 * _MAX_BODY):
                            raise SpoolError("unsafe orphan file")
                        with os.fdopen(fd, "rb", closefd=False) as handle:
                            record = json.load(handle)
                        body = base64.b64decode(record["body"], validate=True)
                        if (record["id"] != name[:-5] or len(body) > _MAX_BODY
                                or not hmac.compare_digest(record["sha256"], hashlib.sha256(body).hexdigest())):
                            raise SpoolError("invalid orphan record")
                        expiry = record.get("expires_at", info.st_mtime + 3600)
                        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                            raise SpoolError("invalid orphan expiry")
                        if now >= min(expiry, info.st_mtime + 3600):
                            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                                raise SpoolError("orphan file changed")
                            os.unlink(name, dir_fd=directory)
                            removed += 1
                    finally:
                        os.close(fd)
                except (OSError, SpoolError, ValueError, TypeError, KeyError, OverflowError, binascii.Error):
                    failures += 1
            if removed:
                os.fsync(directory)
        if failures:
            raise SpoolError(f"{failures} entries could not be cleaned")
        return removed
