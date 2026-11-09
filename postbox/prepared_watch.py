"""Bind a unique payload marker to a local log boundary."""
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import stat


class WatchPreparationError(Exception):
    pass


def new_marker():
    return 'postbox-' + secrets.token_hex(24)


def wire_body(body, marker):
    if (not isinstance(marker, str) or not re.fullmatch(r'postbox-[0-9a-f]{48}', marker)
            or not isinstance(body, str) or not body or marker in body):
        raise WatchPreparationError('valid unique marker and body required')
    return f'[{marker}]\n{body}'


def prepare_watch(*, target, thread, incarnation, recipient_pid, rollout, offset, body, sender_addr, sender_session=None, started_at, marker=None):
    for value in (target, thread, incarnation, sender_addr):
        if not isinstance(value, str) or not value.strip() or len(value) > 1024:
            raise WatchPreparationError('exact target, thread, incarnation and sender route required')
    part = r'[a-z0-9][a-z0-9-]{0,63}'
    if (not re.fullmatch(part + '@' + part + '(?:@' + part + ')?', sender_addr)
            or (sender_session is not None and (not isinstance(sender_session, str) or not re.fullmatch(part, sender_session)))
            or not re.fullmatch(r'[0-9a-f]{64}', incarnation)
            or isinstance(recipient_pid, bool) or not isinstance(recipient_pid, int) or not 1 < recipient_pid <= 2**31 - 1
            or not isinstance(body, str) or not body
            or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
            or isinstance(started_at, bool) or not isinstance(started_at, (int, float))
            or not math.isfinite(started_at) or started_at <= 0):
        raise WatchPreparationError('body, valid offset and start time required')
    try:
        path = Path(rollout)
        if not path.is_absolute() or len(str(path)) > 1024:
            raise WatchPreparationError('absolute log path required')
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            path_info = os.lstat(path)
            if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(path_info.st_mode)
                    or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                    or info.st_size != offset or path_info.st_size != offset):
                raise WatchPreparationError('log file or boundary changed')
        finally:
            os.close(fd)
        marker = new_marker() if marker is None else marker
        marked_body = wire_body(body, marker)
        digest = hashlib.sha256(marked_body.encode('utf-8')).hexdigest()
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise WatchPreparationError('log boundary or body unavailable') from exc
    metadata = {'target': target, 'thread': thread, 'incarnation': incarnation, 'recipient_pid': recipient_pid,
                'watch_id': secrets.token_hex(24), 'log_path': str(path),
                'log_identity': f'{info.st_dev}:{info.st_ino}', 'offset': offset,
                'marker': marker, 'payload_digest': digest, 'sender_addr': sender_addr, 'sender_session': sender_session,
                'started_at': started_at}
    return metadata, marked_body
