"""Orchestrate confirmation using caller supplied trust and storage adapters."""

import asyncio
import hashlib
import hmac
import math
import re
from dataclasses import dataclass

from .confirmation_spool import SpoolError
from .confirmation_content import check_summary


class ConfirmationError(Exception):
    def __init__(self, message, *, state=None, message_id=None, digest=None, request_id=None,
                 existing_state=None, existing_expires_at=None):
        super().__init__(message)
        self.state = state
        self.message_id = message_id
        self.digest = digest
        self.request_id = request_id
        self.existing_state = existing_state
        self.existing_expires_at = existing_expires_at


@dataclass(frozen=True)
class Pending:
    id: str
    nonce: str
    message_id: str
    summary: str
    expires_at: float
    digest: str


class ConfirmationService:
    def __init__(self, ledger, spool, adapter):
        self.ledger, self.spool, self.adapter = ledger, spool, adapter

    def expire_bodies(self):
        removed, errors = 0, []
        now, expired, known = self.ledger.expired_body_records()
        for message_id, digest in expired:
            try:
                removed += int(self.spool.remove_expired(message_id, digest))
            except Exception as exc:
                errors.append(exc)
        for sweep, args in ((self.spool.sweep_crash_temps, (now,)), (self.spool.sweep_orphan_bodies, (now, known))):
            try:
                removed += sweep(*args)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ConfirmationError("expired body cleanup failed", state="cleanup_failed") from errors[0]
        return removed

    async def run_expiry_worker(self, stop, *, interval=30.0):
        if (not isinstance(stop, asyncio.Event) or isinstance(interval, bool)
                or not isinstance(interval, (int, float)) or not math.isfinite(interval) or not 0 < interval <= 60):
            raise ValueError("stop event and bounded interval required")
        while not stop.is_set():
            await asyncio.to_thread(self.expire_bodies)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def _canonical_identity(self, identity):
        identity = self.ledger._identity(identity)
        part = r"[a-z0-9][a-z0-9-]{0,63}"
        if (not re.fullmatch(part + "@" + part + "@" + part + "#" + part, identity[0])
                or self.adapter.is_canonical_identity(identity) is not True):
            raise ConfirmationError("explicit canonical recipient required")
        return identity

    def start(self, identity, summary, body, *, message_id, ttl, original_sent_at=None):
        if original_sent_at is None:
            raise ValueError("original send time required")
        self.expire_bodies()
        identity = self._canonical_identity(identity)
        self.spool._filename(message_id)
        if (not isinstance(summary, str) or not summary.strip() or len(summary) > 500
                or not isinstance(body, bytes) or len(body) > 1024 * 1024
                or isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0):
            raise ValueError("valid summary, body and TTL required")
        check_summary(summary, body)
        if self.adapter.verify_recipient(identity) is not True:
            raise ConfirmationError("recipient unavailable or unverified")
        digest = hashlib.sha256(body).hexdigest()
        try:
            request = self.ledger.create(identity, summary, body, ttl=ttl, message_id=message_id,
                                         exclusive=True, original_sent_at=original_sent_at)
        except Exception as exc:
            try:
                saved = self.ledger.reconcile(message_id, identity, summary, bytes.fromhex(digest))
                state, request_id = "existing_request_reconciled", saved.id
                existing_state, existing_expires_at = saved.state, saved.expires_at
            except Exception:
                state, request_id, existing_state, existing_expires_at = "creation_unknown", None, None, None
            raise ConfirmationError("creation requires reconciliation; do not resend", state=state,
                                    message_id=message_id, digest=digest, request_id=request_id,
                                    existing_state=existing_state, existing_expires_at=existing_expires_at) from exc
        pending = Pending(request.id, request.nonce, request.message_id, request.summary, request.expires_at, digest)
        self.resume_ready(pending, identity, body)
        return pending

    def resume_ready(self, pending, identity, body):
        identity = self._canonical_identity(identity)
        if self.adapter.verify_recipient(identity) is not True:
            raise ConfirmationError("recipient unavailable or changed")
        saved = self._reconcile(pending, identity)
        if saved.state != "pending" or self.ledger.challenge_status(pending.id) != "ready":
            raise ConfirmationError("challenge already attempted or unavailable")
        if not isinstance(body, bytes) or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), pending.digest):
            raise ConfirmationError("body does not match request")
        check_summary(pending.summary, body)
        try:
            self.spool.create(pending.message_id, body, expires_at=pending.expires_at)
        except SpoolError:
            self.spool.read(pending.message_id, pending.digest, expected_expires_at=pending.expires_at)
        try:
            self.ledger.begin_challenge(pending.id)
        except Exception as exc:
            try:
                state = "challenge_uncertain" if self.ledger.challenge_status(pending.id) == "attempted" else "challenge_not_attempted"
            except Exception:
                state = "challenge_unknown"
            raise ConfirmationError("challenge state requires reconciliation; do not resend", state=state) from exc
        try:
            current = self.ledger.status(pending.id)
        except Exception as exc:
            raise ConfirmationError("challenge state unavailable; do not resend", state="challenge_unknown") from exc
        if current != "pending":
            raise ConfirmationError("challenge expired before dispatch")
        try:
            receipt = self.adapter.send_challenge(identity, pending, deadline=pending.expires_at)
            if receipt is not None:
                if (not isinstance(receipt, tuple) or len(receipt) != 4 or receipt[0] != "native_queue"
                        or not isinstance(receipt[3], tuple) or len(receipt[3]) > 64
                        or any(not isinstance(event, dict) for event in receipt[3])):
                    raise ValueError("unrecognized challenge receipt")
                self.ledger.record_challenge_submission(pending.id, identity, receipt[1], server_incarnation=receipt[2])
            if self.ledger.status(pending.id) != "pending":
                raise ValueError("challenge no longer pending")
            if receipt is not None and receipt[3]:
                self.adapter.accept_early_events(identity, pending, receipt[3])
        except Exception as exc:
            raise ConfirmationError("challenge outcome unknown; do not resend", state="challenge_uncertain",
                                    message_id=pending.message_id, digest=pending.digest, request_id=pending.id) from exc

    def confirm_and_send(self, pending, identity, evidence):
        identity = self._canonical_identity(identity)
        if self.adapter.verify_recipient(identity) is not True:
            raise ConfirmationError("recipient unavailable or changed")
        self._reconcile(pending, identity)
        if self.ledger.challenge_status(pending.id) != "attempted":
            raise ConfirmationError("challenge has not been attempted")
        verified = self.adapter.verify_ack(evidence, identity, pending.id, pending.nonce)
        if (not isinstance(verified, tuple) or len(verified) != 2 or verified[0] not in ("accept", "decline")
                or not isinstance(verified[1], str) or not verified[1]):
            raise ConfirmationError("acknowledgement unverified")
        decision, capability = verified
        if self.adapter.verify_saved_ack(capability, identity, pending.id, pending.nonce, decision=decision) is not True:
            raise ConfirmationError("acknowledgement capability unverified")
        try:
            state = self.ledger.confirm_verified(pending.id, identity, pending.nonce, capability, decision=decision)
        except Exception as exc:
            try:
                saved = self.ledger.status(pending.id)
                state = "confirmed_recovery" if saved == "confirmed" else "declined" if saved == "declined" else "decision_unknown"
            except Exception:
                state = "decision_unknown"
            raise ConfirmationError("decision state requires reconciliation; do not resend", state=state) from exc
        if state == "declined":
            return state
        try:
            reference = self.adapter.retain_return_evidence(identity, pending.id, pending.message_id)
            self.ledger.bind_return_reference(pending.id, identity, pending.message_id, reference)
        except Exception as exc:
            raise ConfirmationError("confirmation saved; return evidence requires recovery", state="confirmed_recovery") from exc
        return self.resume_confirmed(pending, identity)

    def _reconcile(self, pending, identity):
        try:
            saved = self.ledger.reconcile(pending.message_id, identity, pending.summary, bytes.fromhex(pending.digest))
            if (saved.id != pending.id or not hmac.compare_digest(saved.nonce, pending.nonce)
                    or saved.expires_at != pending.expires_at):
                raise ConfirmationError("request identity mismatch")
            return saved
        except (AttributeError, TypeError, ValueError) as exc:
            raise ConfirmationError("invalid request") from exc

    def recover(self, identity, message_id, summary, digest):
        identity = self._canonical_identity(identity)
        try:
            saved = self.ledger.reconcile(message_id, identity, summary, bytes.fromhex(digest))
        except (TypeError, ValueError) as exc:
            raise ConfirmationError("invalid recovery details") from exc
        pending = Pending(saved.id, saved.nonce, message_id, summary, saved.expires_at, digest)
        return pending, saved.state, self.ledger.challenge_status(saved.id)

    def resume_confirmed(self, pending, identity):
        identity = self._canonical_identity(identity)
        if self.adapter.verify_recipient(identity) is not True:
            raise ConfirmationError("recipient unavailable or changed")
        if self._reconcile(pending, identity).state != "confirmed" or not self.ledger.return_reference(pending.id):
            raise ConfirmationError("durable confirmation evidence unavailable")
        try:
            capability = self.ledger.confirmation_capability(pending.id)
        except Exception as exc:
            raise ConfirmationError("verified acknowledgement unavailable") from exc
        if self.adapter.verify_saved_ack(capability, identity, pending.id, pending.nonce, decision="accept") is not True:
            raise ConfirmationError("acknowledgement capability unverified")
        body = self.spool.read(pending.message_id, pending.digest, expected_expires_at=pending.expires_at)
        if self.adapter.verify_recipient(identity) is not True:
            raise ConfirmationError("recipient unavailable or changed")
        try:
            body = self.ledger.begin_handoff(pending.id, identity, body)
        except Exception as exc:
            try:
                saved = self.ledger.status(pending.id)
                state = "handoff_uncertain" if saved == "handoff_uncertain" else "handoff_not_started" if saved == "confirmed" else "handoff_unknown"
            except Exception:
                state = "handoff_unknown"
            raise ConfirmationError("handoff state requires reconciliation; do not resend", state=state) from exc
        try:
            receipt = self.adapter.send_body(identity, pending.message_id, body, deadline=pending.expires_at)
            if not isinstance(receipt, str) or not receipt.strip():
                raise ValueError("missing receipt")
            return self.ledger.finish_handoff(pending.id, message_id=pending.message_id, receipt_id=receipt)
        except Exception as exc:
            raise ConfirmationError("handoff outcome unknown; do not resend", state="handoff_uncertain") from exc

    def acknowledge_handoff(self, pending, identity, evidence, receipt_id):
        identity = self._canonical_identity(identity)
        self._reconcile(pending, identity)
        if self.adapter.verify_final_ack(evidence, identity, pending.message_id, receipt_id) is not True:
            raise ConfirmationError("recipient acknowledgement unverified")
        return self.ledger.acknowledge_handoff(pending.id, identity, message_id=pending.message_id,
                                               receipt_id=receipt_id, evidence=evidence)
