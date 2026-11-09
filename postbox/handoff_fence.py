"""Durable reservations that prevent automatic duplicate handoffs."""
import json
import os
import math
import datetime
import hashlib
import hmac
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

try:
    from .handshake import ConfirmationLedger
except ImportError:
    from handshake import ConfirmationLedger


class FenceError(Exception):
    def __init__(self, message, *, reason='fence_error'):
        super().__init__(message)
        self.reason = reason


class RecipientBusy(FenceError):
    def __init__(self):
        super().__init__('recipient slot is occupied', reason='recipient_busy')


@dataclass(frozen=True)
class StreamBinding:
    instance: str
    cutover_sequence: int


class HandoffFence:
    def __init__(self, path):
        self.path = Path(path)
        self._check_directory()
        info = self._check_file()
        self.identity = (info.st_dev, info.st_ino)
        with closing(self._connect()) as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 11:
                raise FenceError('unsupported fence schema')
            db.execute("SELECT stream, sequence, subject, message_id, state, token, watch_json, watch_id, watch_registered, endpoint_json, endpoint_slot, terminal_reason, terminal_detail, confirmation_ledger_id, confirmation_request_id, watch_marker FROM handoffs LIMIT 0")
            db.execute('SELECT stream, sequence, event_id, sender_json, kind, status, reason, detail, remote_receipt, notice_receipt, attempt_token FROM reconciliation LIMIT 0')
            db.execute('SELECT target, stream_name, created_utc, cutover_seq, high_water_seq, instance FROM relay_binding LIMIT 0')
            db.execute('SELECT stream, sequence, event_id, subject, message_id, endpoint_json, watch_id, payload_digest, ledger_id, request_id, identity_json, evidence_digest FROM acknowledgement_releases LIMIT 0')

    @classmethod
    def initialize(cls, path):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(instance.path, flags, 0o600)
        os.close(fd)
        directory_fd = os.open(instance.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        info = instance._check_file()
        instance.identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute("""CREATE TABLE handoffs (
                    stream TEXT NOT NULL, sequence INTEGER NOT NULL,
                    subject TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('uncertain', 'accepted', 'refused')),
                    token TEXT NOT NULL, watch_json TEXT, watch_id TEXT UNIQUE,
                    watch_registered INTEGER NOT NULL DEFAULT 0 CHECK (watch_registered IN (0, 1)),
                    endpoint_json TEXT, endpoint_slot TEXT UNIQUE, terminal_reason TEXT, terminal_detail TEXT,
                    confirmation_ledger_id TEXT, confirmation_request_id TEXT, watch_marker TEXT,
                    PRIMARY KEY (stream, sequence)
                )""")
                db.execute('''CREATE TABLE reconciliation (
                    stream TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                    sender_json TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                    reason TEXT NOT NULL, detail TEXT NOT NULL, remote_receipt TEXT,
                    notice_receipt TEXT UNIQUE, attempt_token TEXT UNIQUE, PRIMARY KEY (stream, sequence),
                    FOREIGN KEY (stream, sequence) REFERENCES handoffs (stream, sequence)
                )''')
                db.execute('''CREATE TABLE relay_binding (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    target TEXT NOT NULL, stream_name TEXT NOT NULL, created_utc TEXT NOT NULL,
                    cutover_seq INTEGER NOT NULL, high_water_seq INTEGER NOT NULL, instance TEXT NOT NULL UNIQUE
                )''')
                HandoffFence._ensure_ack_release_schema(db)
                db.execute('PRAGMA user_version = 11')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return instance

    @classmethod
    def migrate_v9(cls, path):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = instance._check_file()
        instance.identity = (info.st_dev, info.st_ino)
        try:
            with closing(instance._connect()) as db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    if db.execute('PRAGMA user_version').fetchone()[0] != 9:
                        raise FenceError('migration requires a version 9 fence')
                    db.execute('SELECT stream, sequence, subject, message_id, state, token, watch_json, watch_id, watch_registered, endpoint_json, endpoint_slot, terminal_reason, terminal_detail FROM handoffs LIMIT 0')
                    db.execute('SELECT stream, sequence, event_id, sender_json, kind, status, reason, detail, remote_receipt, notice_receipt, attempt_token FROM reconciliation LIMIT 0')
                    db.execute('''CREATE TABLE relay_binding (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        target TEXT NOT NULL, stream_name TEXT NOT NULL, created_utc TEXT NOT NULL,
                        cutover_seq INTEGER NOT NULL, high_water_seq INTEGER NOT NULL, instance TEXT NOT NULL UNIQUE
                    )''')
                    HandoffFence._ensure_ack_release_schema(db)
                    db.execute('PRAGMA user_version = 11')
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
        except sqlite3.Error as exc:
            raise FenceError('fence migration failed') from exc
        return cls(path)

    @staticmethod
    def _create_ack_release_schema(db):
        db.execute('''CREATE TABLE acknowledgement_releases (
            stream TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
            subject TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE, endpoint_json TEXT NOT NULL,
            watch_id TEXT NOT NULL, payload_digest TEXT NOT NULL, ledger_id TEXT NOT NULL,
            request_id TEXT NOT NULL, identity_json TEXT NOT NULL, evidence_digest TEXT NOT NULL,
            PRIMARY KEY (stream, sequence),
            FOREIGN KEY (stream, sequence) REFERENCES handoffs (stream, sequence)
        )''')

    @classmethod
    def _ensure_ack_release_schema(cls, db):
        columns = {row['name'] for row in db.execute('PRAGMA table_info(handoffs)')}
        if 'confirmation_ledger_id' not in columns:
            db.execute('ALTER TABLE handoffs ADD COLUMN confirmation_ledger_id TEXT')
        if 'confirmation_request_id' not in columns:
            db.execute('ALTER TABLE handoffs ADD COLUMN confirmation_request_id TEXT')
        if 'watch_marker' not in columns:
            db.execute('ALTER TABLE handoffs ADD COLUMN watch_marker TEXT')
        seen = set()
        for row in db.execute('SELECT rowid, watch_json, watch_marker FROM handoffs WHERE watch_json IS NOT NULL').fetchall():
            try:
                watch = json.loads(row['watch_json'])
                if (cls._watch(watch) != row['watch_json'] or watch['marker'] in seen
                        or row['watch_marker'] not in (None, watch['marker'])):
                    raise FenceError('existing prepared watch markers are invalid or duplicated')
                seen.add(watch['marker'])
                if row['watch_marker'] is None:
                    db.execute('UPDATE handoffs SET watch_marker=? WHERE rowid=?', (watch['marker'], row['rowid']))
            except (ValueError, TypeError, KeyError) as exc:
                raise FenceError('existing prepared watch metadata is invalid') from exc
        index = next((row for row in db.execute('PRAGMA index_list(handoffs)').fetchall()
                      if row['name'] == 'handoff_watch_marker_unique'), None)
        if index is None:
            db.execute('CREATE UNIQUE INDEX handoff_watch_marker_unique ON handoffs(watch_marker)')
        elif index['unique'] != 1 or [row['name'] for row in db.execute('PRAGMA index_info(handoff_watch_marker_unique)')] != ['watch_marker']:
            raise FenceError('prepared watch marker index is invalid')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='acknowledgement_releases'").fetchone():
            cls._create_ack_release_schema(db)

    @classmethod
    def migrate_v10(cls, path):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = instance._check_file()
        instance.identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('PRAGMA user_version').fetchone()[0] != 10:
                    raise FenceError('migration requires a version 10 fence')
                cls._ensure_ack_release_schema(db)
                db.execute('PRAGMA user_version = 11')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
        return cls(path)

    @staticmethod
    def _stream_evidence(target, info):
        if not isinstance(target, str) or not re.fullmatch(r'[0-9a-f]{64}', target):
            raise FenceError('broker identity unavailable')
        config, state, created = (getattr(info, name, None) for name in ('config', 'state', 'created'))
        name, subjects = getattr(config, 'name', None), getattr(config, 'subjects', None)
        metadata = getattr(config, 'metadata', None)
        marker = metadata.get('postbox_generation') if isinstance(metadata, dict) else None
        retention = getattr(config, 'retention', None)
        retention = getattr(retention, 'value', retention)
        sequence = getattr(state, 'last_seq', None)
        if (name != 'POST' or not isinstance(subjects, (list, tuple))
                or len(subjects) != 2 or any(not isinstance(subject, str) for subject in subjects)
                or set(subjects) != {'post.>', 'forward.>'}
                or retention != 'workqueue' or isinstance(sequence, bool)
                or not isinstance(sequence, int) or not 0 <= sequence <= 2**63 - 1
                or not isinstance(marker, str) or not re.fullmatch(r'[0-9a-f]{32}', marker)
                or not isinstance(created, datetime.datetime) or created.tzinfo is None
                or created.utcoffset() is None):
            raise FenceError('stream evidence unavailable or invalid')
        return target, name, created.astimezone(datetime.timezone.utc).isoformat(), sequence, marker

    def bind_stream(self, target, info):
        target, name, created, sequence, marker = self._stream_evidence(target, info)
        binding = StreamBinding(marker, sequence)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if (db.execute('SELECT 1 FROM relay_binding').fetchone()
                        or db.execute('SELECT 1 FROM handoffs LIMIT 1').fetchone()
                        or db.execute('SELECT 1 FROM reconciliation LIMIT 1').fetchone()):
                    raise FenceError('fence already bound or contains handoffs')
                db.execute('INSERT INTO relay_binding VALUES (1, ?, ?, ?, ?, ?, ?)',
                           (target, name, created, sequence, sequence, binding.instance))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return binding

    def verify_stream(self, target, info, *, expected=None):
        if expected is not None and not isinstance(expected, StreamBinding):
            raise FenceError('invalid expected stream binding')
        target, name, created, sequence, marker = self._stream_evidence(target, info)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('SELECT * FROM relay_binding WHERE singleton = 1').fetchone()
                if (row is None or (row['target'], row['stream_name'], row['created_utc']) != (target, name, created)
                        or sequence < row['high_water_seq'] or row['high_water_seq'] < row['cutover_seq']
                        or row['instance'] != marker or not re.fullmatch(r'[0-9a-f]{32}', row['instance'])
                        or (expected is not None and expected != StreamBinding(row['instance'], row['cutover_seq']))):
                    raise FenceError('stream binding missing, changed or regressed')
                db.execute('UPDATE relay_binding SET high_water_seq = ? WHERE singleton = 1', (sequence,))
                db.execute('COMMIT')
                return StreamBinding(row['instance'], row['cutover_seq'])
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def _check_directory(self):
        info = os.lstat(self.path.parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise FenceError("fence directory must be owned and private")

    def _check_file(self):
        info = os.lstat(self.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise FenceError("fence must be an owned private regular file")
        return info

    def _connect(self, *, read_only=False):
        self._check_directory()
        info = self._check_file()
        if (info.st_dev, info.st_ino) != self.identity:
            raise FenceError("fence file changed")
        mode = 'ro' if read_only else 'rw'
        db = sqlite3.connect(self.path.absolute().as_uri() + '?mode=' + mode, uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        if not read_only:
            db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _key(stream, sequence, subject, message_id):
        if (not all(isinstance(x, str) and x.strip() and len(x) <= 512 for x in (stream, subject, message_id))
                or isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 < sequence <= 2**63 - 1):
            raise FenceError("valid stream, sequence, subject and message id are required")
        return stream, sequence, subject, message_id

    def lookup(self, stream, sequence, subject, message_id):
        key = self._key(stream, sequence, subject, message_id)
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute("SELECT stream, sequence, subject, message_id, state FROM handoffs WHERE (stream=? AND sequence=?) OR message_id=?",
                              (stream, sequence, message_id)).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or tuple(rows[0][field] for field in ('stream', 'sequence', 'subject', 'message_id')) != key:
            raise FenceError('handoff identity mismatch')
        if rows[0]['state'] not in ('uncertain', 'accepted', 'refused'):
            raise FenceError('invalid handoff state')
        return rows[0]['state']

    @staticmethod
    def _watch(watch):
        if watch is None:
            return None
        fields = {'target', 'thread', 'incarnation', 'recipient_pid', 'watch_id', 'log_path', 'log_identity',
                  'offset', 'marker', 'payload_digest', 'sender_addr', 'sender_session', 'started_at'}
        if not isinstance(watch, dict) or set(watch) != fields:
            raise FenceError('invalid prepared watch metadata')
        for name in fields - {'offset', 'started_at', 'recipient_pid', 'sender_session'}:
            value = watch[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise FenceError('invalid prepared watch metadata')
        part = r'[a-z0-9][a-z0-9-]{0,63}'
        sender = watch['sender_addr']
        session = watch['sender_session']
        if (not re.fullmatch(part + '@' + part + '(?:@' + part + ')?', sender)
                or (session is not None and (not isinstance(session, str) or not re.fullmatch(part, session)))
                or not re.fullmatch(r'[0-9a-f]{64}', watch['incarnation'])
                or isinstance(watch['recipient_pid'], bool) or not isinstance(watch['recipient_pid'], int)
                or not 1 < watch['recipient_pid'] <= 2**31 - 1
                or not os.path.isabs(watch['log_path']) or not re.fullmatch(r'[0-9a-f]{64}', watch['payload_digest'])
                or isinstance(watch['offset'], bool) or not isinstance(watch['offset'], int) or watch['offset'] < 0
                or isinstance(watch['started_at'], bool) or not isinstance(watch['started_at'], (int, float))
                or not math.isfinite(watch['started_at']) or watch['started_at'] <= 0):
            raise FenceError('invalid prepared watch metadata')
        return json.dumps(watch, sort_keys=True, separators=(',', ':'))

    @staticmethod
    def _endpoint(endpoint):
        if endpoint is None:
            return None, None
        if not isinstance(endpoint, (tuple, list)) or len(endpoint) != 4:
            raise FenceError('invalid endpoint')
        target, thread, incarnation, pid = endpoint
        part = r'[a-z0-9][a-z0-9-]{0,63}'
        if (not isinstance(target, str) or not re.fullmatch(part + '@' + part + '@' + part + '#' + part, target)
                or not isinstance(thread, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', thread)
                or not isinstance(incarnation, str) or not re.fullmatch(r'[0-9a-f]{64}', incarnation)
                or isinstance(pid, bool) or not isinstance(pid, int) or not 1 < pid <= 2**31 - 1):
            raise FenceError('invalid endpoint')
        encoded = json.dumps([target, thread, incarnation, pid], separators=(',', ':'))
        address = target.split('#', 1)[0].split('@')
        agent = address[0]
        machine = address[2]
        slot = json.dumps([agent, machine, thread], separators=(',', ':'))
        return encoded, slot

    @staticmethod
    def _trusted_confirmation_binding(ledger, request_id, identity, message_id, body_digest, *, states):
        if type(ledger) is not ConfirmationLedger:
            raise FenceError('trusted confirmation ledger required')
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 256
                or not isinstance(message_id, str) or not message_id
                or not isinstance(body_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', body_digest)):
            raise FenceError('confirmation binding is invalid')
        try:
            with closing(ledger._connect(read_only=True)) as db:
                row = db.execute('SELECT state, address, thread, incarnation, message_id, digest FROM confirmations WHERE id=?',
                                 (request_id,)).fetchone()
            if (row is None or row['state'] not in states
                    or identity != (row['address'], row['thread'], row['incarnation'])
                    or row['message_id'] != message_id or not isinstance(row['digest'], bytes)
                    or not hmac.compare_digest(row['digest'].hex(), body_digest)):
                raise FenceError('confirmation ledger row does not match handoff')
            file_identity = tuple(ledger._file_identity)
            ledger_path = str(Path(ledger.path).absolute())
            instance = hashlib.sha256(json.dumps([ledger_path, *file_identity], separators=(',', ':')).encode()).hexdigest()
            state = row['state']
        except FenceError:
            raise
        except Exception as exc:
            raise FenceError('confirmation ledger unavailable') from exc
        return instance, state

    def inspect_endpoint(self, endpoint):
        encoded, slot = self._endpoint(endpoint)
        if encoded is None:
            raise FenceError('endpoint required')
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('SELECT stream, sequence, subject, message_id, state, endpoint_json FROM handoffs WHERE endpoint_slot=?', (slot,)).fetchone()
        if row is None:
            return None
        if row['endpoint_json'] != encoded:
            raise FenceError('endpoint identity mismatch')
        return tuple(row[field] for field in ('stream', 'sequence', 'subject', 'message_id')), row['state']

    @staticmethod
    def _advance_bound_sequence(db, stream, sequence):
        binding = db.execute('SELECT instance, cutover_seq, high_water_seq FROM relay_binding WHERE singleton = 1').fetchone()
        if binding is None:
            return
        if stream != binding['instance'] or sequence <= binding['cutover_seq']:
            raise FenceError('handoff does not belong to the bound post-cutover stream')
        if sequence > binding['high_water_seq']:
            db.execute('UPDATE relay_binding SET high_water_seq = ? WHERE singleton = 1', (sequence,))

    def reserve(self, stream, sequence, subject, message_id, *, watch=None, endpoint=None,
                confirmation_ledger=None, confirmation_request_id=None):
        key = self._key(stream, sequence, subject, message_id)
        prepared = self._watch(watch)
        endpoint_json, endpoint_slot = self._endpoint(endpoint)
        ledger_id = None
        ledger_state = None
        request_id = None
        if (confirmation_ledger is None) != (confirmation_request_id is None):
            raise FenceError('confirmation ledger and request id must be supplied together')
        if confirmation_ledger is not None:
            if watch is None or endpoint is None or prepared is None:
                raise FenceError('confirmation handoff requires an exact prepared watch and endpoint')
            request_id = confirmation_request_id
            ledger_id, ledger_state = self._trusted_confirmation_binding(
                confirmation_ledger, request_id, (watch['target'], watch['thread'], watch['incarnation']),
                message_id, watch['payload_digest'], states={'confirmed', 'handoff_uncertain', 'handed_off', 'acknowledged'})
        if watch is not None and endpoint is not None and tuple(endpoint) != (watch['target'], watch['thread'], watch['incarnation'], watch['recipient_pid']):
            raise FenceError('watch and endpoint identity mismatch')
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._advance_bound_sequence(db, stream, sequence)
                row = db.execute("SELECT subject, message_id, state, watch_json, endpoint_json, endpoint_slot, confirmation_ledger_id, confirmation_request_id, watch_marker FROM handoffs WHERE stream=? AND sequence=?", key[:2]).fetchone()
                if row:
                    if (row['subject'], row['message_id']) != key[2:]:
                        raise FenceError("stream sequence identity mismatch")
                    if row['state'] != 'refused' and row['watch_json'] != prepared:
                        raise FenceError('prepared watch metadata mismatch')
                    if row['state'] != 'refused' and row['endpoint_json'] != endpoint_json:
                        raise FenceError('endpoint identity mismatch')
                    if row['state'] != 'refused' and row['endpoint_slot'] != endpoint_slot:
                        released = db.execute('SELECT 1 FROM acknowledgement_releases WHERE stream=? AND sequence=? AND message_id=?',
                                              (*key[:2], message_id)).fetchone()
                        if not (row['state'] == 'accepted' and row['endpoint_slot'] is None and released):
                            raise FenceError('endpoint slot state mismatch')
                    if row['state'] != 'refused' and (row['confirmation_ledger_id'], row['confirmation_request_id']) != (ledger_id, request_id):
                        raise FenceError('confirmation ledger identity mismatch')
                    result = (row['state'], None)
                else:
                    if confirmation_ledger is not None and ledger_state != 'confirmed':
                        raise FenceError('confirmation handoff is no longer eligible for first reservation')
                    if db.execute('SELECT 1 FROM handoffs WHERE message_id=?', (message_id,)).fetchone():
                        raise FenceError('message id already reserved under another stream sequence', reason='duplicate_message_id')
                    if endpoint_slot is not None and db.execute('SELECT 1 FROM handoffs WHERE endpoint_slot=?', (endpoint_slot,)).fetchone():
                        raise RecipientBusy()
                    if watch is not None and db.execute('SELECT 1 FROM handoffs WHERE watch_id=?', (watch['watch_id'],)).fetchone():
                        raise FenceError('watch id already reserved', reason='duplicate_watch_id')
                    if watch is not None and db.execute('SELECT 1 FROM handoffs WHERE watch_marker=?', (watch['marker'],)).fetchone():
                        raise FenceError('prepared watch marker already reserved', reason='duplicate_watch_marker')
                    token = secrets.token_hex(32)
                    try:
                        db.execute("""INSERT INTO handoffs
                            (stream, sequence, subject, message_id, state, token, watch_json, watch_id,
                             watch_registered, endpoint_json, endpoint_slot, terminal_reason, terminal_detail,
                             confirmation_ledger_id, confirmation_request_id, watch_marker)
                            VALUES (?, ?, ?, ?, 'uncertain', ?, ?, ?, 0, ?, ?, NULL, NULL, ?, ?, ?)""",
                                   (*key, token, prepared, watch['watch_id'] if watch else None,
                                    endpoint_json, endpoint_slot, ledger_id, request_id, watch['marker'] if watch else None))
                    except sqlite3.IntegrityError as exc:
                        raise FenceError('fence reservation constraint failed', reason='reservation_constraint') from exc
                    result = ("new", token)
                db.execute("COMMIT")
                return result
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def cancel_before_send(self, stream, sequence, subject, message_id, token):
        key = self._key(stream, sequence, subject, message_id)
        if not isinstance(token, str) or not token:
            raise FenceError('reservation token required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                cursor = db.execute("UPDATE handoffs SET state='refused', endpoint_slot=NULL, terminal_reason='expired_before_handoff', terminal_detail='Expired before transport started' WHERE stream=? AND sequence=? AND subject=? AND message_id=? AND token=? AND state='uncertain'", (*key, token))
                if cursor.rowcount != 1:
                    raise FenceError('reservation does not match an uncertain handoff')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def claim_terminal(self, stream, sequence, subject, message_id, *, reason, detail=''):
        key = self._key(stream, sequence, subject, message_id)
        if not isinstance(reason, str) or not re.fullmatch(r'[a-z0-9_]{1,64}', reason):
            raise FenceError('invalid terminal reason')
        if not isinstance(detail, str):
            raise FenceError('invalid terminal detail')
        detail = ''.join(c if c.isprintable() else ' ' for c in detail)
        detail = ' '.join(detail.split())[:1024] or reason
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._advance_bound_sequence(db, stream, sequence)
                rows = db.execute('SELECT stream, sequence, subject, message_id, state FROM handoffs WHERE (stream=? AND sequence=?) OR message_id=?',
                                  (stream, sequence, message_id)).fetchall()
                if rows:
                    if len(rows) != 1 or tuple(rows[0][field] for field in ('stream', 'sequence', 'subject', 'message_id')) != key:
                        raise FenceError('handoff identity mismatch')
                    result = rows[0]['state']
                else:
                    db.execute("""INSERT INTO handoffs
                        (stream, sequence, subject, message_id, state, token, watch_json, watch_id,
                         watch_registered, endpoint_json, endpoint_slot, terminal_reason, terminal_detail,
                         confirmation_ledger_id, confirmation_request_id, watch_marker)
                        VALUES (?, ?, ?, ?, 'refused', ?, NULL, NULL, 0, NULL, NULL, ?, ?, NULL, NULL, NULL)""",
                               (*key, secrets.token_hex(32), reason, detail))
                    result = 'claimed'
                db.execute('COMMIT')
                return result
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def terminal_reason(self, stream, sequence, subject, message_id):
        return self.terminal_record(stream, sequence, subject, message_id)[0]

    def terminal_record(self, stream, sequence, subject, message_id):
        key = self._key(stream, sequence, subject, message_id)
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute('SELECT stream, sequence, subject, message_id, state, terminal_reason, terminal_detail FROM handoffs WHERE (stream=? AND sequence=?) OR message_id=?',
                              (stream, sequence, message_id)).fetchall()
        if (len(rows) != 1 or tuple(rows[0][field] for field in ('stream', 'sequence', 'subject', 'message_id')) != key
                or rows[0]['state'] != 'refused' or not isinstance(rows[0]['terminal_reason'], str)
                or not re.fullmatch(r'[a-z0-9_]{1,64}', rows[0]['terminal_reason'])
                or not isinstance(rows[0]['terminal_detail'], str) or not rows[0]['terminal_detail']
                or len(rows[0]['terminal_detail']) > 1024 or not rows[0]['terminal_detail'].isprintable()):
            raise FenceError('refusal identity or reason unavailable')
        return rows[0]['terminal_reason'], rows[0]['terminal_detail']

    def _prepare_notice(self, key, sender, kind, status=None, reason=None, detail=None, remote_receipt=None):
        sender_json, _ = self._endpoint(sender)
        if sender_json is None:
            raise FenceError('exact sender endpoint required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                handoff = db.execute('SELECT * FROM handoffs WHERE stream=? AND sequence=?', key[:2]).fetchone()
                if not handoff or (handoff['subject'], handoff['message_id']) != key[2:]:
                    raise FenceError('handoff identity mismatch')
                if kind == 'local_refusal':
                    if handoff['state'] != 'refused':
                        raise FenceError('no recorded refusal')
                    status, reason, detail, remote_receipt = 'refused', handoff['terminal_reason'], handoff['terminal_detail'], None
                elif kind == 'forward_result':
                    if handoff['state'] != 'accepted':
                        raise FenceError('forward acceptance not recorded')
                    if (status not in {'delivered', 'refused', 'queued', 'held', 'unknown'}
                            or not isinstance(remote_receipt, str) or not re.fullmatch(r'[0-9a-f]{32,64}', remote_receipt)):
                        raise FenceError('invalid forward outcome')
                    reason = 'remote_reported'
                    detail = {
                        'delivered': 'Remote relay reported handoff acceptance; recipient acknowledgement remains unverified.',
                        'refused': 'Remote relay reported refusal.',
                        'queued': 'Remote relay reported a queued message.',
                        'held': 'Remote relay reported a held message.',
                        'unknown': 'Remote relay reported an unknown outcome.',
                    }[status]
                else:
                    raise FenceError('invalid notice kind')
                row = db.execute('SELECT * FROM reconciliation WHERE stream=? AND sequence=?', key[:2]).fetchone()
                values = (sender_json, kind, status, reason, detail, remote_receipt)
                if row:
                    if tuple(row[field] for field in ('sender_json', 'kind', 'status', 'reason', 'detail', 'remote_receipt')) != values:
                        raise FenceError('reconciliation details mismatch')
                    event_id = row['event_id']
                else:
                    event_id = secrets.token_hex(32)
                    db.execute('INSERT INTO reconciliation VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)', (*key[:2], event_id, *values))
                db.execute('COMMIT')
                return event_id
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def prepare_refusal_notice(self, stream, sequence, subject, message_id, *, sender):
        return self._prepare_notice(self._key(stream, sequence, subject, message_id), sender, 'local_refusal')

    def prepare_forward_notice(self, stream, sequence, subject, message_id, *, sender, status, remote_receipt):
        return self._prepare_notice(self._key(stream, sequence, subject, message_id), sender, 'forward_result', status, remote_receipt=remote_receipt)

    def pending_notices(self):
        return self._list_notices(uncertain=False)

    def uncertain_notices(self):
        return self._list_notices(uncertain=True)

    def _list_notices(self, *, uncertain):
        with closing(self._connect(read_only=True)) as db:
            condition = 'IS NOT NULL' if uncertain else 'IS NULL'
            rows = db.execute('''SELECT h.stream, h.sequence, h.subject, h.message_id, r.* FROM handoffs h
                JOIN reconciliation r ON r.stream=h.stream AND r.sequence=h.sequence
                WHERE r.notice_receipt IS NULL AND r.attempt_token ''' + condition + ' ORDER BY h.stream, h.sequence').fetchall()
        out = []
        for row in rows:
            try:
                sender = tuple(json.loads(row['sender_json']))
                if self._endpoint(sender)[0] != row['sender_json']:
                    raise FenceError('invalid sender endpoint')
                key = self._key(*(row[field] for field in ('stream', 'sequence', 'subject', 'message_id')))
            except (ValueError, TypeError, KeyError) as exc:
                raise FenceError('invalid reconciliation record') from exc
            out.append({'id': row['event_id'], 'key': key, 'sender': sender, 'kind': row['kind'],
                        'status': row['status'], 'reason': row['reason'], 'detail': row['detail'],
                        'remote_receipt': row['remote_receipt']})
        return out

    def notice_status(self, stream, sequence, subject, message_id):
        key = self._key(stream, sequence, subject, message_id)
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute('''SELECT h.stream, h.sequence, h.subject, h.message_id, r.event_id, r.notice_receipt
                FROM handoffs h LEFT JOIN reconciliation r ON r.stream=h.stream AND r.sequence=h.sequence
                WHERE (h.stream=? AND h.sequence=?) OR h.message_id=?''', (stream, sequence, message_id)).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or tuple(rows[0][field] for field in ('stream', 'sequence', 'subject', 'message_id')) != key:
            raise FenceError('handoff identity mismatch')
        return (rows[0]['event_id'], rows[0]['notice_receipt']) if rows[0]['event_id'] else None

    def begin_notice_attempt(self, stream, sequence, subject, message_id, *, event_id, sender):
        key = self._key(stream, sequence, subject, message_id)
        sender_json, _ = self._endpoint(sender)
        if sender_json is None:
            raise FenceError('exact sender required')
        token = secrets.token_hex(32)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('''SELECT h.subject, h.message_id, r.event_id, r.sender_json, r.attempt_token, r.notice_receipt
                    FROM handoffs h JOIN reconciliation r ON r.stream=h.stream AND r.sequence=h.sequence
                    WHERE h.stream=? AND h.sequence=?''', key[:2]).fetchone()
                if (not row or (row['subject'], row['message_id']) != key[2:] or row['event_id'] != event_id
                        or row['sender_json'] != sender_json or row['attempt_token'] is not None or row['notice_receipt'] is not None):
                    raise FenceError('notice attempt unavailable or identity mismatch')
                db.execute('UPDATE reconciliation SET attempt_token=? WHERE stream=? AND sequence=?', (token, *key[:2]))
                db.execute('COMMIT')
                return token
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def record_notice_acceptance(self, stream, sequence, subject, message_id, *, event_id, sender, attempt_token, receipt):
        key = self._key(stream, sequence, subject, message_id)
        sender_json, _ = self._endpoint(sender)
        if sender_json is None or not isinstance(receipt, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', receipt):
            raise FenceError('exact sender and notification receipt required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('''SELECT h.subject, h.message_id, r.event_id, r.sender_json, r.notice_receipt, r.attempt_token
                    FROM handoffs h JOIN reconciliation r ON r.stream=h.stream AND r.sequence=h.sequence
                    WHERE h.stream=? AND h.sequence=?''', key[:2]).fetchone()
                if (not row or (row['subject'], row['message_id']) != key[2:] or row['event_id'] != event_id
                        or row['sender_json'] != sender_json or not isinstance(attempt_token, str) or row['attempt_token'] != attempt_token
                        or (row['notice_receipt'] is not None and row['notice_receipt'] != receipt)):
                    raise FenceError('notification acceptance mismatch')
                db.execute('UPDATE reconciliation SET notice_receipt=? WHERE stream=? AND sequence=?', (receipt, *key[:2]))
                db.execute('COMMIT')
            except sqlite3.IntegrityError as exc:
                db.execute('ROLLBACK')
                raise FenceError('notification receipt already used') from exc
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def pending_watches(self):
        with closing(self._connect(read_only=True)) as db:
            rows = db.execute("SELECT stream, sequence, subject, message_id, state, watch_json, watch_id FROM handoffs WHERE state IN ('uncertain', 'accepted') AND watch_json IS NOT NULL AND watch_registered=0 ORDER BY stream, sequence").fetchall()
        result = []
        for row in rows:
            try:
                watch = json.loads(row['watch_json'])
                if self._watch(watch) != row['watch_json'] or watch['watch_id'] != row['watch_id']:
                    raise FenceError('invalid prepared watch record')
            except (ValueError, TypeError, KeyError) as exc:
                raise FenceError('invalid prepared watch record') from exc
            key = tuple(row[field] for field in ('stream', 'sequence', 'subject', 'message_id'))
            result.append((key, row['state'], watch))
        return result

    def mark_watch_registered(self, stream, sequence, subject, message_id, *, watch_id):
        key = self._key(stream, sequence, subject, message_id)
        if not isinstance(watch_id, str) or not watch_id.strip():
            raise FenceError('watch id required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                row = db.execute('SELECT subject, message_id, state, watch_id, watch_json FROM handoffs WHERE stream=? AND sequence=?', key[:2]).fetchone()
                if not row or (row['subject'], row['message_id']) != key[2:] or row['state'] not in ('uncertain', 'accepted') or row['watch_id'] != watch_id or not row['watch_json']:
                    raise FenceError('watch identity mismatch')
                try:
                    prepared = json.loads(row['watch_json'])
                    if self._watch(prepared) != row['watch_json'] or prepared['watch_id'] != watch_id:
                        raise FenceError('invalid prepared watch record')
                except (ValueError, TypeError, KeyError) as exc:
                    raise FenceError('invalid prepared watch record') from exc
                db.execute('UPDATE handoffs SET watch_registered=1 WHERE stream=? AND sequence=?', key[:2])
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise

    def accept(self, stream, sequence, subject, message_id, token):
        key = self._key(stream, sequence, subject, message_id)
        if not isinstance(token, str) or not token:
            raise FenceError("reservation token required")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute("UPDATE handoffs SET state='accepted' WHERE stream=? AND sequence=? AND subject=? AND message_id=? AND token=? AND state='uncertain'", (*key, token))
                if cursor.rowcount != 1:
                    raise FenceError("reservation does not match an uncertain handoff")
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def release_after_ack(self, stream, sequence, subject, message_id, *, endpoint, confirmation_ledger,
                          confirmation_request_id, identity, receipt_id, evidence, body_digest):
        raise FenceError('authenticated host ACK authority unavailable; recipient slot remains occupied')
