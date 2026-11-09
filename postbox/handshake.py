"""Durable confirmation state and evidence checks for exact recipients."""
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from contextlib import closing
import math

try:
    from .confirmation_content import check_summary
except ImportError:
    from confirmation_content import check_summary

MAX_CONFIRMATION_TTL_S = 3600


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Request:
    id: str
    message_id: str
    nonce: str
    summary: str
    expires_at: float


@dataclass(frozen=True)
class ReconciledRequest:
    id: str
    nonce: str
    expires_at: float
    state: str


class ConfirmationLedger:
    @classmethod
    def initialize(cls, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None):
        return cls(path, clock=clock, canonical_identity=canonical_identity, ack_verifier=ack_verifier,
                   final_ack_verifier=final_ack_verifier, _initialize=True)

    @classmethod
    def migrate_v8(cls, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = os.lstat(instance.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('confirmation database must be a private owned regular file')
        instance._file_identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('PRAGMA application_id').fetchone()[0] != 1346525233 or db.execute('PRAGMA user_version').fetchone()[0] != 8:
                    raise Refused('migration requires a version 8 confirmation ledger')
                db.execute('SELECT id, nonce, address, thread, incarnation, summary, digest, expires, state, message_id, receipt_id, expiry_receipt, recipient_slot, exclusive FROM confirmations LIMIT 0')
                db.execute('SELECT request_id, state, ack_capability FROM challenges LIMIT 0')
                db.execute('SELECT request_id, reference FROM return_evidence LIMIT 0')
                db.execute('SELECT id, last FROM ledger_clock LIMIT 0')
                db.execute('CREATE TABLE final_ack_evidence (request_id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
                db.execute("CREATE TABLE challenge_submissions (request_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, cleanup_state TEXT NOT NULL DEFAULT 'ready' CHECK(cleanup_state IN ('ready','uncertain','deleted','not_in_queue')), server_incarnation TEXT)")
                db.execute('CREATE TABLE native_dispatch (request_id TEXT PRIMARY KEY, server_incarnation TEXT)')
                db.execute("INSERT INTO native_dispatch SELECT h.request_id, s.server_incarnation FROM challenges h LEFT JOIN challenge_submissions s ON s.request_id=h.request_id WHERE h.state='attempted'")
                db.execute('PRAGMA user_version = 13')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return cls(path, clock=clock, canonical_identity=canonical_identity, ack_verifier=ack_verifier,
                   final_ack_verifier=final_ack_verifier)

    @classmethod
    def migrate_v9(cls, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = os.lstat(instance.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('confirmation database must be a private owned regular file')
        instance._file_identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('PRAGMA application_id').fetchone()[0] != 1346525233 or db.execute('PRAGMA user_version').fetchone()[0] != 9:
                    raise Refused('migration requires a version 9 confirmation ledger')
                db.execute('SELECT id, nonce, address, thread, incarnation, summary, digest, expires, state, message_id, receipt_id, expiry_receipt, recipient_slot, exclusive FROM confirmations LIMIT 0')
                db.execute('SELECT request_id, state, ack_capability FROM challenges LIMIT 0')
                db.execute('SELECT request_id, reference FROM return_evidence LIMIT 0')
                db.execute('SELECT request_id, digest FROM final_ack_evidence LIMIT 0')
                db.execute('SELECT id, last FROM ledger_clock LIMIT 0')
                db.execute("CREATE TABLE challenge_submissions (request_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, cleanup_state TEXT NOT NULL DEFAULT 'ready' CHECK(cleanup_state IN ('ready','uncertain','deleted','not_in_queue')), server_incarnation TEXT)")
                db.execute('CREATE TABLE native_dispatch (request_id TEXT PRIMARY KEY, server_incarnation TEXT)')
                db.execute("INSERT INTO native_dispatch SELECT h.request_id, s.server_incarnation FROM challenges h LEFT JOIN challenge_submissions s ON s.request_id=h.request_id WHERE h.state='attempted'")
                db.execute('PRAGMA user_version = 13')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return cls(path, clock=clock, canonical_identity=canonical_identity, ack_verifier=ack_verifier,
                   final_ack_verifier=final_ack_verifier)

    @classmethod
    def migrate_v10(cls, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = os.lstat(instance.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('confirmation database must be a private owned regular file')
        instance._file_identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('PRAGMA application_id').fetchone()[0] != 1346525233 or db.execute('PRAGMA user_version').fetchone()[0] != 10:
                    raise Refused('migration requires a version 10 confirmation ledger')
                db.execute('SELECT id, nonce, address, thread, incarnation, summary, digest, expires, state, message_id, receipt_id, expiry_receipt, recipient_slot, exclusive FROM confirmations LIMIT 0')
                db.execute('SELECT request_id, state, ack_capability FROM challenges LIMIT 0')
                db.execute('SELECT request_id, reference FROM return_evidence LIMIT 0')
                db.execute('SELECT request_id, digest FROM final_ack_evidence LIMIT 0')
                db.execute('SELECT id, last FROM ledger_clock LIMIT 0')
                db.execute('SELECT request_id, submission_id FROM challenge_submissions LIMIT 0')
                db.execute("ALTER TABLE challenge_submissions ADD COLUMN cleanup_state TEXT NOT NULL DEFAULT 'ready' CHECK(cleanup_state IN ('ready','uncertain','deleted','not_in_queue'))")
                db.execute('ALTER TABLE challenge_submissions ADD COLUMN server_incarnation TEXT')
                db.execute('CREATE TABLE native_dispatch (request_id TEXT PRIMARY KEY, server_incarnation TEXT)')
                db.execute("INSERT INTO native_dispatch SELECT h.request_id, s.server_incarnation FROM challenges h LEFT JOIN challenge_submissions s ON s.request_id=h.request_id WHERE h.state='attempted'")
                db.execute('PRAGMA user_version = 13')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return cls(path, clock=clock, canonical_identity=canonical_identity, ack_verifier=ack_verifier,
                   final_ack_verifier=final_ack_verifier)

    @classmethod
    def migrate_v11(cls, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = os.lstat(instance.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('confirmation database must be a private owned regular file')
        instance._file_identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('PRAGMA application_id').fetchone()[0] != 1346525233 or db.execute('PRAGMA user_version').fetchone()[0] != 11:
                    raise Refused('migration requires a version 11 confirmation ledger')
                db.execute('SELECT id, nonce, address, thread, incarnation, summary, digest, expires, state, message_id, receipt_id, expiry_receipt, recipient_slot, exclusive FROM confirmations LIMIT 0')
                db.execute('SELECT request_id, state, ack_capability FROM challenges LIMIT 0')
                db.execute('SELECT request_id, reference FROM return_evidence LIMIT 0')
                db.execute('SELECT request_id, digest FROM final_ack_evidence LIMIT 0')
                db.execute('SELECT id, last FROM ledger_clock LIMIT 0')
                db.execute('SELECT request_id, submission_id, cleanup_state FROM challenge_submissions LIMIT 0')
                db.execute('ALTER TABLE challenge_submissions ADD COLUMN server_incarnation TEXT')
                db.execute('CREATE TABLE native_dispatch (request_id TEXT PRIMARY KEY, server_incarnation TEXT)')
                db.execute("INSERT INTO native_dispatch SELECT h.request_id, s.server_incarnation FROM challenges h LEFT JOIN challenge_submissions s ON s.request_id=h.request_id WHERE h.state='attempted'")
                db.execute('PRAGMA user_version = 13')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return cls(path, clock=clock, canonical_identity=canonical_identity, ack_verifier=ack_verifier,
                   final_ack_verifier=final_ack_verifier)

    @classmethod
    def migrate_v12(cls, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None):
        instance = object.__new__(cls)
        instance.path = Path(path)
        instance._check_directory()
        info = os.lstat(instance.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('confirmation database must be a private owned regular file')
        instance._file_identity = (info.st_dev, info.st_ino)
        with closing(instance._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                if db.execute('PRAGMA application_id').fetchone()[0] != 1346525233 or db.execute('PRAGMA user_version').fetchone()[0] != 12:
                    raise Refused('migration requires a version 12 confirmation ledger')
                db.execute('SELECT id, nonce, address, thread, incarnation, summary, digest, expires, state, message_id, receipt_id, expiry_receipt, recipient_slot, exclusive FROM confirmations LIMIT 0')
                db.execute('SELECT request_id, state, ack_capability FROM challenges LIMIT 0')
                db.execute('SELECT request_id, reference FROM return_evidence LIMIT 0')
                db.execute('SELECT request_id, digest FROM final_ack_evidence LIMIT 0')
                db.execute('SELECT request_id, submission_id, cleanup_state, server_incarnation FROM challenge_submissions LIMIT 0')
                db.execute('SELECT id, last FROM ledger_clock LIMIT 0')
                db.execute('CREATE TABLE native_dispatch (request_id TEXT PRIMARY KEY, server_incarnation TEXT)')
                db.execute("INSERT INTO native_dispatch SELECT h.request_id, s.server_incarnation FROM challenges h LEFT JOIN challenge_submissions s ON s.request_id=h.request_id WHERE h.state='attempted'")
                db.execute('PRAGMA user_version = 13')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return cls(path, clock=clock, canonical_identity=canonical_identity, ack_verifier=ack_verifier,
                   final_ack_verifier=final_ack_verifier)

    def __init__(self, path, *, clock=time.time, canonical_identity=None, ack_verifier=None, final_ack_verifier=None, _initialize=False):
        self.path = Path(path)
        self.clock = clock
        self.canonical_identity = canonical_identity
        self.ack_verifier = ack_verifier
        self.final_ack_verifier = final_ack_verifier
        self._check_directory()
        if _initialize:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, 'O_NOFOLLOW'):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags, 0o600)
            os.close(fd)
        info = os.lstat(self.path)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('confirmation database must be a private owned regular file')
        self._file_identity = (info.st_dev, info.st_ino)
        with closing(self._connect()) as db:
            if _initialize:
                db.execute('BEGIN IMMEDIATE')
                try:
                    db.execute('''CREATE TABLE confirmations (
                        id TEXT PRIMARY KEY, nonce TEXT NOT NULL, address TEXT NOT NULL,
                        thread TEXT NOT NULL, incarnation TEXT NOT NULL, summary TEXT NOT NULL,
                        digest BLOB NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                        message_id TEXT NOT NULL UNIQUE, receipt_id TEXT UNIQUE,
                        expiry_receipt TEXT UNIQUE, recipient_slot TEXT NOT NULL, exclusive INTEGER NOT NULL
                    )''')
                    db.execute("CREATE UNIQUE INDEX confirmation_active_recipient ON confirmations(recipient_slot) WHERE exclusive=1 AND state IN ('pending','confirmed','handoff_uncertain','handed_off','expired_uncertain','identity_changed')")
                    db.execute("CREATE TABLE challenges (request_id TEXT PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('ready','attempted')), ack_capability TEXT)")
                    db.execute('CREATE TABLE return_evidence (request_id TEXT PRIMARY KEY, reference TEXT NOT NULL UNIQUE)')
                    db.execute('CREATE TABLE final_ack_evidence (request_id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
                    db.execute("CREATE TABLE challenge_submissions (request_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE, cleanup_state TEXT NOT NULL DEFAULT 'ready' CHECK(cleanup_state IN ('ready','uncertain','deleted','not_in_queue')), server_incarnation TEXT)")
                    db.execute('CREATE TABLE native_dispatch (request_id TEXT PRIMARY KEY, server_incarnation TEXT)')
                    db.execute('CREATE TABLE ledger_clock (id INTEGER PRIMARY KEY CHECK (id = 1), last REAL NOT NULL)')
                    db.execute('PRAGMA application_id = 1346525233')
                    db.execute('PRAGMA user_version = 13')
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
            try:
                app_id = db.execute('PRAGMA application_id').fetchone()[0]
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if app_id != 1346525233 or version != 13:
                    raise Refused('invalid or uninitialized confirmation ledger')
                db.execute('SELECT id, nonce, address, thread, incarnation, summary, digest, expires, state, message_id, receipt_id, expiry_receipt, recipient_slot, exclusive FROM confirmations LIMIT 0')
                db.execute('SELECT request_id, state, ack_capability FROM challenges LIMIT 0')
                db.execute('SELECT request_id, reference FROM return_evidence LIMIT 0')
                db.execute('SELECT request_id, digest FROM final_ack_evidence LIMIT 0')
                db.execute('SELECT request_id, submission_id, cleanup_state, server_incarnation FROM challenge_submissions LIMIT 0')
                db.execute('SELECT request_id, server_incarnation FROM native_dispatch LIMIT 0')
                db.execute('SELECT id, last FROM ledger_clock LIMIT 0')
            except sqlite3.DatabaseError as exc:
                raise Refused('invalid or uninitialized confirmation ledger') from exc

    def _check_directory(self):
        info = os.lstat(self.path.parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError('confirmation directory must be owned and mode 0700')

    def _connect(self, *, read_only=False):
        self._check_directory()
        try:
            info = os.lstat(self.path)
        except FileNotFoundError as exc:
            raise PermissionError('confirmation database missing') from exc
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != self._file_identity):
            raise PermissionError('confirmation database changed or is not private')
        mode = 'ro' if read_only else 'rw'
        db = sqlite3.connect(self.path.absolute().as_uri() + '?mode=' + mode, uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _identity(identity):
        if not isinstance(identity, (tuple, list)) or len(identity) != 3 or any(not isinstance(x, str) or not x.strip() for x in identity):
            raise ValueError('exact address, thread and incarnation are required')
        return tuple(identity)

    def _check_clock(self, db):
        now = self.clock()
        if not isinstance(now, (float, int)) or not math.isfinite(now):
            raise Refused('invalid clock')
        row = db.execute('SELECT last FROM ledger_clock WHERE id = 1').fetchone()
        if row and now < row['last']:
            raise Refused('clock moved backwards')
        db.execute('INSERT INTO ledger_clock VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET last=excluded.last', (now,))
        return now

    def expired_body_records(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                rows = db.execute('SELECT message_id, digest FROM confirmations WHERE expires <= ?', (now,)).fetchall()
                known = {row['message_id'] for row in db.execute('SELECT message_id FROM confirmations')}
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return now, [(row['message_id'], bytes(row['digest']).hex()) for row in rows], known

    def create(self, identity, summary, body, *, ttl, message_id, exclusive=True, original_sent_at=None):
        identity = self._identity(identity)
        part = r'[a-z0-9][a-z0-9-]{0,63}'
        if exclusive is not True or not re.fullmatch(part + '@' + part + '@' + part + '#' + part, identity[0]):
            raise ValueError('exclusive canonical machine-qualified address required')
        if not callable(self.canonical_identity) or self.canonical_identity(identity) is not True:
            raise Refused('canonical recipient identity is unverified')
        addr = identity[0].split('#', 1)[0].split('@')
        if len(addr) not in (2, 3) or any(not p for p in addr):
            raise ValueError('exact agent and project address required')
        slot = json.dumps([addr[0], addr[2] if len(addr) == 3 else '', identity[1]], separators=(',', ':'))
        if (not isinstance(summary, str) or not summary.strip() or len(summary) > 500 or not isinstance(body, bytes)
                or not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 256
                or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0):
            raise ValueError('bounded summary and message id, byte body and positive TTL are required')
        check_summary(summary, body)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                original = now if original_sent_at is None else original_sent_at
                if (isinstance(original, bool) or not isinstance(original, (int, float))
                        or not math.isfinite(original) or original < 0 or original > now):
                    raise Refused('invalid original send time')
                deadline = min(now + min(ttl, MAX_CONFIRMATION_TTL_S), original + MAX_CONFIRMATION_TTL_S)
                if now >= deadline:
                    raise Refused('message already expired')
                db.execute("UPDATE confirmations SET state=CASE WHEN EXISTS (SELECT 1 FROM challenges WHERE request_id=confirmations.id AND state='attempted') THEN 'expired_uncertain' ELSE 'expired' END WHERE state IN ('pending','confirmed') AND expires <= ?", (now,))
                active = db.execute("SELECT exclusive FROM confirmations WHERE recipient_slot=? AND state IN ('pending','confirmed','handoff_uncertain','handed_off','expired_uncertain','identity_changed')", (slot,)).fetchall()
                if active and (exclusive or any(row['exclusive'] for row in active)):
                    raise Refused('recipient already has an active confirmation')
                request = Request(secrets.token_hex(16), message_id, secrets.token_urlsafe(32), summary,
                                  deadline)
                if not math.isfinite(request.expires_at):
                    raise ValueError('invalid expiry')
                db.execute('INSERT INTO confirmations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)',
                           (request.id, request.nonce, *identity, summary, hashlib.sha256(body).digest(), request.expires_at, 'pending', message_id, slot, int(exclusive)))
                db.execute("INSERT INTO challenges VALUES (?, 'ready', NULL)", (request.id,))
                db.execute('COMMIT')
            except sqlite3.IntegrityError as exc:
                db.execute('ROLLBACK')
                raise Refused('message already registered') from exc
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return request

    def begin_challenge(self, request_id):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT state, expires FROM confirmations WHERE id=?', (request_id,)).fetchone()
                if not row or row['state'] != 'pending' or now >= row['expires']:
                    raise Refused('challenge unavailable or expired')
                updated = db.execute("UPDATE challenges SET state='attempted' WHERE request_id=? AND state='ready'", (request_id,))
                if updated.rowcount != 1:
                    raise Refused('challenge already attempted')
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def challenge_status(self, request_id):
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('SELECT state FROM challenges WHERE request_id=?', (request_id,)).fetchone()
        if not row or row['state'] not in ('ready', 'attempted'):
            raise Refused('challenge record unavailable')
        return row['state']

    def reserve_native_challenge_dispatch(self, request_id, identity, server_incarnation):
        identity = self._identity(identity)
        if not isinstance(server_incarnation, str) or not re.fullmatch(r'[0-9a-f]{64}', server_incarnation):
            raise Refused('verified server incarnation required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT c.address, c.thread, c.incarnation, c.state, c.expires, h.state AS challenge_state '
                                 'FROM confirmations c JOIN challenges h ON h.request_id=c.id WHERE c.id=?',
                                 (request_id,)).fetchone()
                if (not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity
                        or row['state'] != 'pending' or now >= row['expires'] or row['challenge_state'] != 'attempted'
                        or db.execute('SELECT 1 FROM challenge_submissions WHERE request_id=?', (request_id,)).fetchone()):
                    raise Refused('exact pending attempted challenge required')
                db.execute('INSERT INTO native_dispatch VALUES (?, ?)', (request_id, server_incarnation))
                db.execute('COMMIT')
            except sqlite3.IntegrityError as exc:
                db.execute('ROLLBACK')
                raise Refused('native challenge dispatch already attempted') from exc
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def record_challenge_submission(self, request_id, identity, submission_id, *, server_incarnation):
        identity = self._identity(identity)
        if (not isinstance(submission_id, str) or not re.fullmatch(r'[!-~]{1,256}', submission_id)
                or not isinstance(server_incarnation, str) or not re.fullmatch(r'[0-9a-f]{64}', server_incarnation)):
            raise ValueError('bounded submission id required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._check_clock(db)
                row = db.execute('SELECT c.address, c.thread, c.incarnation, h.state AS challenge_state '
                                 'FROM confirmations c JOIN challenges h ON h.request_id=c.id WHERE c.id=?',
                                 (request_id,)).fetchone()
                if not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity or row['challenge_state'] != 'attempted':
                    raise Refused('exact attempted challenge required')
                dispatch = db.execute('SELECT server_incarnation FROM native_dispatch WHERE request_id=?', (request_id,)).fetchone()
                if not dispatch or dispatch['server_incarnation'] != server_incarnation:
                    raise Refused('native dispatch server differs or is unknown')
                old = db.execute('SELECT submission_id, server_incarnation FROM challenge_submissions WHERE request_id=?', (request_id,)).fetchone()
                if old:
                    if old['submission_id'] != submission_id or old['server_incarnation'] != server_incarnation:
                        raise Refused('challenge submission differs')
                else:
                    db.execute('INSERT INTO challenge_submissions(request_id, submission_id, server_incarnation) VALUES (?, ?, ?)', (request_id, submission_id, server_incarnation))
                db.execute('COMMIT')
            except sqlite3.IntegrityError as exc:
                db.execute('ROLLBACK')
                raise Refused('submission id already bound') from exc
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def challenge_submission(self, request_id, identity):
        identity = self._identity(identity)
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('SELECT c.address, c.thread, c.incarnation, s.submission_id '
                             'FROM confirmations c JOIN challenge_submissions s ON s.request_id=c.id WHERE c.id=?',
                             (request_id,)).fetchone()
        if not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity:
            raise Refused('exact submission id unavailable')
        return row['submission_id']

    def challenge_submission_binding(self, request_id, identity):
        identity = self._identity(identity)
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('SELECT c.address, c.thread, c.incarnation, c.nonce, c.summary, s.submission_id, '
                             's.server_incarnation, d.server_incarnation AS dispatch_server, h.state AS challenge_state '
                             'FROM confirmations c JOIN challenge_submissions s ON s.request_id=c.id '
                             'JOIN native_dispatch d ON d.request_id=c.id '
                             'JOIN challenges h ON h.request_id=c.id WHERE c.id=?', (request_id,)).fetchone()
        if (not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity
                or row['challenge_state'] != 'attempted' or not row['server_incarnation']
                or row['server_incarnation'] != row['dispatch_server']):
            raise Refused('exact submission binding unavailable')
        return row['submission_id'], row['server_incarnation'], row['nonce'], row['summary']

    def begin_challenge_cleanup(self, request_id, identity, *, server_incarnation):
        identity = self._identity(identity)
        if not isinstance(server_incarnation, str) or not re.fullmatch(r'[0-9a-f]{64}', server_incarnation):
            raise Refused('verified server incarnation required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT c.address, c.thread, c.incarnation, c.expires, '
                                 's.submission_id, s.cleanup_state, s.server_incarnation, h.state AS challenge_state FROM confirmations c '
                                 'JOIN challenge_submissions s ON s.request_id=c.id '
                                 'JOIN challenges h ON h.request_id=c.id WHERE c.id=?',
                                 (request_id,)).fetchone()
                if (not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity
                        or now < row['expires'] or row['cleanup_state'] != 'ready' or row['challenge_state'] != 'attempted'
                        or row['server_incarnation'] != server_incarnation):
                    raise Refused('exact expired submission unavailable or cleanup already attempted')
                db.execute("UPDATE challenge_submissions SET cleanup_state='uncertain' WHERE request_id=?", (request_id,))
                db.execute('COMMIT')
                return row['submission_id']
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def expired_challenge_submissions(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('bounded cleanup limit required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                rows = db.execute("SELECT c.id, c.address, c.thread, c.incarnation, s.submission_id "
                                  "FROM confirmations c JOIN challenge_submissions s ON s.request_id=c.id "
                                  "JOIN challenges h ON h.request_id=c.id "
                                  "WHERE c.expires<=? AND s.cleanup_state='ready' AND h.state='attempted' AND s.server_incarnation IS NOT NULL ORDER BY c.expires, c.id LIMIT ?",
                                  (now, limit)).fetchall()
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return [(row['id'], (row['address'], row['thread'], row['incarnation']), row['submission_id']) for row in rows]

    def challenge_cleanup_status(self, request_id, identity):
        identity = self._identity(identity)
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('SELECT c.address, c.thread, c.incarnation, s.cleanup_state '
                             'FROM confirmations c JOIN challenge_submissions s ON s.request_id=c.id WHERE c.id=?',
                             (request_id,)).fetchone()
        if not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity:
            raise Refused('exact submission cleanup unavailable')
        return row['cleanup_state']

    def record_challenge_cleanup(self, request_id, identity, submission_id, *, deleted):
        identity = self._identity(identity)
        if type(deleted) is not bool:
            raise ValueError('native deletion result must be boolean')
        result = 'deleted' if deleted else 'not_in_queue'
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._check_clock(db)
                row = db.execute('SELECT c.address, c.thread, c.incarnation, s.submission_id, s.cleanup_state '
                                 'FROM confirmations c JOIN challenge_submissions s ON s.request_id=c.id WHERE c.id=?',
                                 (request_id,)).fetchone()
                if (not row or tuple(row[k] for k in ('address', 'thread', 'incarnation')) != identity
                        or row['submission_id'] != submission_id or row['cleanup_state'] not in ('uncertain', result)):
                    raise Refused('exact attempted cleanup required')
                db.execute('UPDATE challenge_submissions SET cleanup_state=? WHERE request_id=?', (result, request_id))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def confirmation_capability(self, request_id):
        with closing(self._connect(read_only=True)) as db:
            row = db.execute("SELECT c.state, h.state AS challenge_state, h.ack_capability FROM confirmations c JOIN challenges h ON h.request_id=c.id WHERE c.id=?", (request_id,)).fetchone()
        if not row or row['state'] != 'confirmed' or row['challenge_state'] != 'attempted' or not row['ack_capability']:
            raise Refused('verified acknowledgement unavailable')
        return row['ack_capability']

    def bind_return_reference(self, request_id, identity, message_id, reference):
        identity = self._identity(identity)
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 256:
            raise ValueError('return reference required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT * FROM confirmations WHERE id=?', (request_id,)).fetchone()
                if (not row or row['state'] != 'confirmed' or now >= row['expires'] or identity != (row['address'], row['thread'], row['incarnation'])
                        or row['message_id'] != message_id):
                    raise Refused('return reference identity mismatch')
                prior = db.execute('SELECT reference FROM return_evidence WHERE request_id=?', (request_id,)).fetchone()
                if prior and prior['reference'] != reference:
                    raise Refused('return reference changed')
                if not prior:
                    db.execute('INSERT INTO return_evidence VALUES (?, ?)', (request_id, reference))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def return_reference(self, request_id):
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('SELECT reference FROM return_evidence WHERE request_id=?', (request_id,)).fetchone()
        return row['reference'] if row else None

    def confirm_verified(self, request_id, identity, nonce, capability, *, decision='accept'):
        identity = self._identity(identity)
        if decision not in ('accept', 'decline') or not isinstance(capability, str) or not capability or len(capability) > 4096:
            raise ValueError('bounded acknowledgement capability required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT c.*, h.state AS challenge_state FROM confirmations c JOIN challenges h ON h.request_id=c.id WHERE c.id=?', (request_id,)).fetchone()
                if (not row or row['state'] != 'pending' or row['challenge_state'] != 'attempted' or now >= row['expires']
                        or identity != (row['address'], row['thread'], row['incarnation'])
                        or not isinstance(nonce, str) or not hmac.compare_digest(nonce, row['nonce'])):
                    raise Refused('verified confirmation does not match')
                if not callable(self.ack_verifier) or self.ack_verifier(capability, identity, request_id, nonce, decision=decision) is not True:
                    raise Refused('decision capability unverified')
                db.execute("UPDATE challenges SET ack_capability=? WHERE request_id=?", (capability, request_id))
                state = 'confirmed' if decision == 'accept' else 'declined'
                db.execute('UPDATE confirmations SET state=? WHERE id=?', (state, request_id))
                db.execute('COMMIT')
                return state
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def reconcile(self, message_id, identity, summary, digest):
        identity = self._identity(identity)
        if (not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 256
                or not isinstance(summary, str) or not summary.strip() or len(summary) > 500
                or not isinstance(digest, bytes) or len(digest) != 32):
            raise ValueError('message id, summary and SHA-256 digest required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT c.*, h.state AS challenge_state FROM confirmations c JOIN challenges h ON h.request_id=c.id WHERE c.message_id = ?', (message_id,)).fetchone()
                if (row is None or identity != (row['address'], row['thread'], row['incarnation'])
                        or summary != row['summary'] or not hmac.compare_digest(digest, row['digest'])):
                    raise Refused('request details do not match')
                state = row['state']
                if state in ('pending', 'confirmed') and now >= row['expires']:
                    state = 'expired_uncertain' if row['challenge_state'] == 'attempted' else 'expired'
                    db.execute('UPDATE confirmations SET state = ? WHERE id = ?', (state, row['id']))
                result = ReconciledRequest(row['id'], row['nonce'], row['expires'], state)
                db.execute('COMMIT')
                return result
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def _transition(self, request_id, action):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            now = self._check_clock(db)
            row = db.execute('SELECT c.*, h.state AS challenge_state, h.ack_capability, e.reference AS return_reference FROM confirmations c JOIN challenges h ON h.request_id=c.id LEFT JOIN return_evidence e ON e.request_id=c.id WHERE c.id = ?', (request_id,)).fetchone()
            if row is None:
                raise Refused('unknown request')
            state, result = action(row, now)
            if state != row['state']:
                db.execute('UPDATE confirmations SET state = ? WHERE id = ?', (state, request_id))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()
        if isinstance(result, Refused):
            raise result
        return result

    def confirm(self, request_id, identity, nonce, *, accept=True):
        identity = self._identity(identity)
        if not isinstance(accept, bool):
            raise ValueError('accept must be a boolean')
        if not accept:
            raise Refused('decline requires verified decision capability')
        def apply(row, now):
            if row['state'] != 'pending':
                return row['state'], Refused('request is no longer pending')
            if now >= row['expires']:
                return ('expired_uncertain' if row['challenge_state'] == 'attempted' else 'expired'), Refused('confirmation expired')
            if identity != (row['address'], row['thread'], row['incarnation']) or not isinstance(nonce, str) or not hmac.compare_digest(nonce, row['nonce']):
                return row['state'], Refused('identity or nonce mismatch')
            state = 'confirmed' if accept is True else 'declined'
            return state, state
        return self._transition(request_id, apply)

    def begin_handoff(self, request_id, current_identity, body):
        current_identity = self._identity(current_identity)
        if not isinstance(body, bytes):
            raise ValueError('body must be bytes')
        def apply(row, now):
            if row['state'] != 'confirmed':
                return row['state'], Refused('request is not confirmed or handoff already started')
            if now >= row['expires']:
                return ('expired_uncertain' if row['challenge_state'] == 'attempted' else 'expired'), Refused('confirmation expired')
            if current_identity != (row['address'], row['thread'], row['incarnation']):
                return 'identity_changed', Refused('recipient identity changed')
            if (row['challenge_state'] != 'attempted' or not row['ack_capability'] or not callable(self.ack_verifier)
                    or self.ack_verifier(row['ack_capability'], current_identity, row['id'], row['nonce'], decision='accept') is not True):
                return row['state'], Refused('accepted acknowledgement unverified')
            if not row['return_reference']:
                return row['state'], Refused('durable return reference unavailable')
            if not hmac.compare_digest(hashlib.sha256(body).digest(), row['digest']):
                return row['state'], Refused('body differs from confirmed request')
            return 'handoff_uncertain', body
        return self._transition(request_id, apply)

    def handoff_remaining(self, request_id, current_identity):
        current_identity = self._identity(current_identity)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT state, address, thread, incarnation, expires FROM confirmations WHERE id=?', (request_id,)).fetchone()
                if (not row or row['state'] != 'handoff_uncertain'
                        or current_identity != (row['address'], row['thread'], row['incarnation'])):
                    raise Refused('handoff identity or state changed')
                remaining = row['expires'] - now
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return max(0.0, remaining)

    def finish_handoff(self, request_id, *, message_id, receipt_id):
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 256:
            raise ValueError('transport receipt id required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._check_clock(db)
                row = db.execute('SELECT state, message_id FROM confirmations WHERE id = ?', (request_id,)).fetchone()
                if row is None or row['state'] != 'handoff_uncertain' or row['message_id'] != message_id:
                    raise Refused('receipt does not match a handoff')
                db.execute('UPDATE confirmations SET state = ?, receipt_id = ? WHERE id = ?', ('handed_off', receipt_id, request_id))
                db.execute('COMMIT')
            except sqlite3.IntegrityError as exc:
                db.execute('ROLLBACK')
                raise Refused('receipt already used') from exc
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return 'handed_off'

    @staticmethod
    def _final_evidence_digest(evidence):
        try:
            encoded = json.dumps(evidence, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError) as exc:
            raise Refused('final acknowledgement evidence is invalid') from exc
        if evidence is None or not encoded or len(encoded) > 16384:
            raise Refused('final acknowledgement evidence is unavailable or too large')
        return hashlib.sha256(encoded).hexdigest()

    def acknowledge_handoff(self, request_id, identity, *, message_id, receipt_id, evidence=None):
        identity = self._identity(identity)
        digest = self._final_evidence_digest(evidence)
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._check_clock(db)
                row = db.execute('SELECT state, address, thread, incarnation, message_id, receipt_id FROM confirmations WHERE id=?', (request_id,)).fetchone()
                if (not row or row['state'] not in ('handed_off', 'acknowledged') or identity != (row['address'], row['thread'], row['incarnation'])
                        or row['message_id'] != message_id or row['receipt_id'] != receipt_id):
                    raise Refused('handoff acknowledgement mismatch')
                if not callable(self.final_ack_verifier) or self.final_ack_verifier(evidence, identity, message_id, receipt_id) is not True:
                    raise Refused('recipient acknowledgement unverified')
                if row['state'] == 'acknowledged':
                    saved = db.execute('SELECT digest FROM final_ack_evidence WHERE request_id=?', (request_id,)).fetchone()
                    if not saved or not isinstance(saved['digest'], str) or not hmac.compare_digest(saved['digest'], digest):
                        raise Refused('final acknowledgement evidence does not match')
                else:
                    db.execute('INSERT INTO final_ack_evidence VALUES (?, ?)', (request_id, digest))
                    db.execute("UPDATE confirmations SET state='acknowledged' WHERE id=?", (request_id,))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return 'acknowledged'

    def has_acknowledged_handoff(self, request_id, identity, *, message_id, receipt_id, evidence=None, body_digest=None):
        identity = self._identity(identity)
        if any(not isinstance(value, str) or not value or len(value) > 256
               for value in (request_id, message_id, receipt_id)):
            raise ValueError('bounded request, message and receipt ids required')
        if body_digest is not None and (not isinstance(body_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', body_digest)):
            raise ValueError('valid body digest required')
        try:
            digest = self._final_evidence_digest(evidence)
        except Refused:
            return False
        with closing(self._connect(read_only=True)) as db:
            row = db.execute('''SELECT c.state, c.address, c.thread, c.incarnation, c.message_id, c.receipt_id,
                                       c.digest AS body_digest, e.digest AS evidence_digest
                                FROM confirmations c JOIN final_ack_evidence e ON e.request_id=c.id WHERE c.id=?''',
                             (request_id,)).fetchone()
        return bool(row and row['state'] == 'acknowledged'
                    and identity == (row['address'], row['thread'], row['incarnation'])
                    and row['message_id'] == message_id and row['receipt_id'] == receipt_id
                    and (body_digest is None or (isinstance(row['body_digest'], bytes) and hmac.compare_digest(row['body_digest'].hex(), body_digest)))
                    and isinstance(row['evidence_digest'], str) and hmac.compare_digest(row['evidence_digest'], digest)
                    and callable(self.final_ack_verifier)
                    and self.final_ack_verifier(evidence, identity, message_id, receipt_id) is True)

    def expire(self):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                db.execute("UPDATE confirmations SET state=CASE WHEN EXISTS (SELECT 1 FROM challenges WHERE request_id=confirmations.id AND state='attempted') THEN 'expired_uncertain' ELSE 'expired' END WHERE state IN ('pending', 'confirmed') AND expires <= ?", (now,))
                rows = db.execute("SELECT id FROM confirmations WHERE state IN ('expired','expired_uncertain') AND expiry_receipt IS NULL ORDER BY id").fetchall()
                db.execute('COMMIT')
                return [row['id'] for row in rows]
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def ack_expiry(self, request_id, *, receipt_id):
        if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 256:
            raise ValueError('notification receipt id required')
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                self._check_clock(db)
                row = db.execute('SELECT state, expiry_receipt FROM confirmations WHERE id = ?', (request_id,)).fetchone()
                if row is None or row['state'] not in ('expired', 'expired_uncertain'):
                    raise Refused('request is not expired')
                if row['expiry_receipt'] is not None and row['expiry_receipt'] != receipt_id:
                    raise Refused('expiry already acknowledged with a different receipt')
                db.execute('UPDATE confirmations SET expiry_receipt = ? WHERE id = ?', (receipt_id, request_id))
                db.execute('COMMIT')
            except sqlite3.IntegrityError as exc:
                db.execute('ROLLBACK')
                raise Refused('expiry receipt already used') from exc
            except BaseException:
                db.execute('ROLLBACK')
                raise
        return 'acknowledged'

    def status(self, request_id):
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                now = self._check_clock(db)
                row = db.execute('SELECT c.state, c.expires, h.state AS challenge_state FROM confirmations c JOIN challenges h ON h.request_id=c.id WHERE c.id = ?', (request_id,)).fetchone()
                if row is None:
                    raise Refused('unknown request')
                state = row['state']
                if state in ('pending', 'confirmed') and now >= row['expires']:
                    state = 'expired_uncertain' if row['challenge_state'] == 'attempted' else 'expired'
                db.execute('COMMIT')
                return state
            except BaseException:
                db.execute('ROLLBACK')
                raise
