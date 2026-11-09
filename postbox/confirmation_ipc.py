"""Serve one bounded confirmation request over a verified local socket."""

import base64
import json
import math
import time

from .recipient_identity import _peer_pid

_LIMIT = 16384
_FIELDS = {"type", "request_id", "nonce", "message_id", "summary", "digest", "evidence"}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("invalid JSON constant")


def recover_return_reference(connection, ledger, verifier, request_id, message_id, *, timeout=3.0, peer_lookup=_peer_pid):
    if (verifier is None or getattr(verifier, "enforces_deadline", None) is not True
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 10
            or not isinstance(request_id, str) or not request_id or len(request_id) > 256
            or not isinstance(message_id, str) or not message_id or len(message_id) > 256):
        raise ValueError("verified bounded recovery required")
    deadline = time.monotonic() + timeout
    peer = peer_lookup(connection)
    if isinstance(peer, bool) or not isinstance(peer, int) or peer <= 1:
        raise ValueError("peer identity unavailable")
    identity = ledger._identity(verifier.identify(connection, peer, deadline=deadline))
    reference = verifier.lookup_return_evidence(connection, identity, request_id, message_id, deadline=deadline)
    if (not isinstance(reference, str) or not reference.strip() or len(reference) > 256
            or verifier.verify_retained_return_evidence(connection, identity, request_id, message_id, reference, deadline=deadline) is not True
            or peer_lookup(connection) != peer or verifier.revalidate(connection, peer, identity, deadline=deadline) is not True
            or time.monotonic() >= deadline):
        raise ValueError("matching retained evidence unavailable")
    ledger.bind_return_reference(request_id, identity, message_id, reference)
    return reference


def serve_once(connection, ledger, spool, verifier=None, *, timeout=3.0, peer_lookup=_peer_pid):
    attempted, phase, request_id = False, "initial", None
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 10:
            raise ValueError("invalid timeout")
        connection.settimeout(timeout)
        deadline = time.monotonic() + timeout
        if verifier is None or getattr(verifier, "enforces_deadline", None) is not True:
            raise ValueError("session verifier unavailable")
        peer = peer_lookup(connection)
        if isinstance(peer, bool) or not isinstance(peer, int) or peer <= 1:
            raise ValueError("peer identity unavailable")
        identity = ledger._identity(verifier.identify(connection, peer, deadline=deadline))
        buf = bytearray()
        while b"\n" not in buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request timed out")
            connection.settimeout(remaining)
            chunk = connection.recv(min(4096, _LIMIT + 1 - len(buf)))
            if not chunk:
                raise ValueError("incomplete request")
            buf.extend(chunk)
            if len(buf) > _LIMIT:
                raise ValueError("request too large")
        line, trailing = bytes(buf).split(b"\n", 1)
        if trailing:
            raise ValueError("one request only")
        request = json.loads(line, object_pairs_hook=_object, parse_constant=_reject_constant)
        if (not isinstance(request, dict) or set(request) != _FIELDS or request["type"] != "confirm"
                or not isinstance(request["evidence"], dict)
                or any(not isinstance(request[key], str) or not request[key] for key in _FIELDS - {"evidence"})):
            raise ValueError("invalid request")
        saved = ledger.reconcile(request["message_id"], identity, request["summary"], bytes.fromhex(request["digest"]))
        request_id = saved.id
        if saved.id != request["request_id"] or saved.nonce != request["nonce"] or saved.state != "pending":
            raise ValueError("request mismatch")
        if ledger.challenge_status(saved.id) != "attempted":
            raise ValueError("challenge not attempted")
        result = verifier.confirm(connection, identity, saved.id, saved.nonce, request["evidence"], deadline=deadline)
        if not isinstance(result, tuple) or len(result) != 2 or result[0] not in ("accept", "decline"):
            raise ValueError("confirmation unverified")
        decision, capability = result
        if peer_lookup(connection) != peer or verifier.revalidate(connection, peer, identity, deadline=deadline) is not True:
            raise ValueError("recipient changed")
        if decision == "decline":
            phase = "decision_attempted"
            ledger.confirm_verified(saved.id, identity, saved.nonce, capability, decision="decline")
            phase = "declined"
            if time.monotonic() >= deadline:
                raise TimeoutError("request timed out")
            connection.settimeout(deadline - time.monotonic())
            connection.sendall(b'{"type":"result","status":"declined"}\n')
            return "declined"
        reference = verifier.retain_return_evidence(connection, identity, saved.id, request["message_id"], deadline=deadline)
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 256:
            raise ValueError("return evidence unavailable")
        phase = "decision_attempted"
        ledger.confirm_verified(saved.id, identity, saved.nonce, capability)
        phase = "confirmed"
        ledger.bind_return_reference(saved.id, identity, request["message_id"], reference)
        body = spool.read(request["message_id"], request["digest"], expected_expires_at=saved.expires_at)
        if peer_lookup(connection) != peer or verifier.revalidate(connection, peer, identity, deadline=deadline) is not True:
            raise ValueError("recipient changed")
        if time.monotonic() >= deadline:
            raise TimeoutError("request timed out")
        connection.settimeout(deadline - time.monotonic())
        body = ledger.begin_handoff(saved.id, identity, body)
        attempted = True
        response = {"type": "body", "request_id": saved.id, "message_id": request["message_id"],
                    "return_reference": reference, "body_b64": base64.b64encode(body).decode("ascii")}
        encoded = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if peer_lookup(connection) != peer or verifier.revalidate(connection, peer, identity, deadline=deadline) is not True:
            return "handoff_uncertain"
        started = time.monotonic()
        handoff_deadline = min(deadline, started + ledger.handoff_remaining(saved.id, identity))
        if (time.monotonic() >= handoff_deadline or peer_lookup(connection) != peer
                or verifier.revalidate(connection, peer, identity, deadline=handoff_deadline) is not True):
            return "handoff_uncertain"
        connection.settimeout(handoff_deadline - time.monotonic())
        connection.sendall(encoded)
        return "handoff_uncertain"
    except Exception:
        if attempted:
            return "handoff_uncertain"
        if phase == "declined":
            return "declined_notice_unknown"
        result = "refused"
        if phase in ("decision_attempted", "confirmed"):
            try:
                state = ledger.status(request_id)
            except Exception:
                state = "unknown"
            result = ("confirmed_recovery" if state == "confirmed" else
                      "declined_notice_unknown" if state == "declined" else
                      "confirmation_unknown" if state in ("pending", "unknown") else state)
        try:
            connection.sendall(json.dumps({"type": "result", "status": result}).encode() + b"\n")
        except Exception:
            pass
        return result
    finally:
        try:
            connection.close()
        except Exception:
            pass
