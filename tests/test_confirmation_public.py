#!/usr/bin/env python3
"""Synthetic checks for bounded confirmation helpers and private body storage."""

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
from postbox.confirmation_client import ConfirmationClientError, retrieve_confirmed
from postbox.confirmation_content import check_summary
from postbox.confirmation_ipc import serve_once
from postbox.confirmation_spool import ConfirmationSpool, SpoolError


class FakeSocket:
    def __init__(self, response=b""):
        self.response = bytearray(response)
        self.sent = bytearray()
        self.timeout = 2.0
        self.closed = False

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, value):
        self.sent.extend(value)

    def recv(self, size):
        value = bytes(self.response[:size])
        del self.response[:size]
        return value

    def close(self):
        self.closed = True


def check(label, condition):
    print(f"  {'ok' if condition else 'FAIL'}   {label}")
    return bool(condition)


def main():
    passed = failed = 0

    with tempfile.TemporaryDirectory(prefix="confirm-check-") as temp:
        os.chmod(temp, 0o700)
        spool = ConfirmationSpool(temp)
        body = b"synthetic message body"
        digest = spool.create("message_1", body, expires_at=9999999999)
        ok = spool.read("message_1", digest, expected_expires_at=9999999999) == body
        ok &= spool.remove_expired("message_1", digest) is True
        try:
            spool.read("message_1", digest)
            ok = False
        except SpoolError:
            pass
        passed += check("spool stores, verifies and removes a synthetic body", ok)
        failed += not ok

    try:
        check_summary("short summary", b"a longer synthetic body")
        summary_ok = True
    except ValueError:
        summary_ok = False
    try:
        check_summary("complete synthetic body", b"complete synthetic body")
        summary_ok = False
    except ValueError:
        pass
    passed += check("summary rejects a complete body disclosure", summary_ok)
    failed += not summary_ok

    body = b"synthetic body"
    digest = hashlib.sha256(body).hexdigest()
    response = {"type": "body", "request_id": "req_1", "message_id": "msg_1",
                "body_b64": base64.b64encode(body).decode(), "return_reference": "ref-1"}
    sock = FakeSocket(json.dumps(response, separators=(",", ":")).encode() + b"\n")
    result = retrieve_confirmed(sock, request_id="req_1", nonce="nonce_1", message_id="msg_1",
                                summary="short summary", digest=digest, evidence={"answer": "yes"},
                                verify_connection=lambda conn: True, verify_invocation=lambda request: True)
    ok = result.body == body and result.return_reference == "ref-1" and sock.timeout == 2.0
    passed += check("client verifies response body and restores socket timeout", ok)
    failed += not ok

    bad = dict(response, body_b64=base64.b64encode(b"different body").decode())
    try:
        retrieve_confirmed(FakeSocket(json.dumps(bad).encode() + b"\n"), request_id="req_1", nonce="nonce_1",
                           message_id="msg_1", summary="short summary", digest=digest, evidence={},
                           verify_connection=lambda conn: True, verify_invocation=lambda request: True)
        ok = False
    except ConfirmationClientError:
        ok = True
    passed += check("client refuses a body with the wrong digest", ok)
    failed += not ok

    refused = FakeSocket()
    outcome = serve_once(refused, None, None, verifier=None, peer_lookup=lambda conn: 10)
    try:
        refusal = json.loads(bytes(refused.sent).splitlines()[0])
    except (IndexError, ValueError):
        refusal = None
    ok = outcome == "refused" and refused.closed and refusal == {"type": "result", "status": "refused"}
    passed += check("IPC refuses requests without an explicit trusted verifier", ok)
    failed += not ok

    print(f"\n{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
