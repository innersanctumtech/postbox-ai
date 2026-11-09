"""Bounded read-only lookup of delivery and recipient-observation evidence."""
import re
import json
from pathlib import Path

MAX_SCAN_BYTES = 8 * 1024 * 1024
ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def lookup(path, message_id):
    if not isinstance(message_id, str) or not ID.fullmatch(message_id):
        raise ValueError('invalid message id')
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - MAX_SCAN_BYTES))
            if size > MAX_SCAN_BYTES:
                f.readline()
            records = f.read(MAX_SCAN_BYTES).splitlines()
    except OSError:
        return {'state': 'unknown', 'delivery_ids': []}
    matches, seen = set(), set()
    for line in records:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get('event') == 'delivery_correlation' and event.get('message_id') == message_id:
            ids = event.get('delivery_ids')
            if isinstance(ids, list) and all(isinstance(i, str) and ID.fullmatch(i) for i in ids):
                matches.add(tuple(ids))
        if event.get('event') in ('recipient_seen',) and isinstance(event.get('id'), str):
            seen.add(event['id'])
    if len(matches) != 1:
        return {'state': 'unknown', 'delivery_ids': []}
    ids = list(next(iter(matches)))
    if not ids:
        return {'state': 'unknown', 'delivery_ids': []}
    return {'state': 'appeared' if all(i in seen for i in ids) else 'handoff', 'delivery_ids': ids}
