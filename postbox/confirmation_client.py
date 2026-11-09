"""Retrieve one bounded confirmation response over a caller-verified socket."""

import base64
import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import dataclass


class ConfirmationClientError(Exception):
    pass


class ConfirmationDeclined(ConfirmationClientError):
    """The verified peer returned an explicit decline."""


@dataclass(frozen=True)
class ConfirmationBody:
    body: bytes
    return_reference: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def retrieve_confirmed(connection, *, request_id, nonce, message_id, summary, digest, evidence,
                       verify_connection=None, verify_invocation=None, timeout=5.0, max_response_bytes=1400000):
    """Send one request and return verified body bytes; this function never retries."""
    token = r"[A-Za-z0-9_-]{1,128}"
    valid = (
        all(isinstance(value, str) and re.fullmatch(token, value) for value in (request_id, nonce, message_id))
        and isinstance(summary, str) and bool(summary.strip()) and len(summary) <= 500
        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        and isinstance(evidence, dict) and callable(verify_connection) and callable(verify_invocation)
        and not isinstance(timeout, bool) and isinstance(timeout, (int, float)) and math.isfinite(timeout) and 0 < timeout <= 30
        and not isinstance(max_response_bytes, bool) and isinstance(max_response_bytes, int)
        and 128 <= max_response_bytes <= 1400000
    )
    if not valid:
        raise ConfirmationClientError("invalid request or missing trust verification")
    request = {"type": "confirm", "request_id": request_id, "nonce": nonce, "message_id": message_id,
               "summary": summary, "digest": digest, "evidence": evidence}
    try:
        encoded = json.dumps(request, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
        if len(encoded) > 16384 or verify_connection(connection) is not True or verify_invocation(request) is not True:
            raise ValueError("verification failed")
        if json.dumps(request, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n" != encoded:
            raise ValueError("request changed")
        previous_timeout = connection.gettimeout()
    except Exception:
        raise ConfirmationClientError("request could not be verified") from None
    deadline = time.monotonic() + timeout
    try:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(encoded)
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response deadline")
            connection.settimeout(remaining)
            data = connection.recv(min(65536, max_response_bytes + 1 - len(chunks)))
            if not data:
                break
            chunks.extend(data)
            if len(chunks) > max_response_bytes:
                raise ValueError("response too large")
        if chunks[-1:] != b"\n" or chunks.count(b"\n") != 1:
            raise ValueError("invalid response framing")
        response = json.loads(chunks[:-1], object_pairs_hook=_unique_object)
        if response == {"type": "result", "status": "declined"}:
            raise ConfirmationDeclined("recipient declined the request")
        if (not isinstance(response, dict) or set(response) != {"type", "request_id", "message_id", "body_b64", "return_reference"}
                or response["type"] != "body" or response["request_id"] != request_id or response["message_id"] != message_id
                or not isinstance(response["body_b64"], str) or not isinstance(response["return_reference"], str)
                or not response["return_reference"].strip() or len(response["return_reference"]) > 256
                or not response["return_reference"].isprintable()):
            raise ValueError("response not accepted")
        body = base64.b64decode(response["body_b64"], validate=True)
        if len(body) > 1024 * 1024 or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), digest):
            raise ValueError("body mismatch")
        return ConfirmationBody(body, response["return_reference"])
    except ConfirmationDeclined:
        raise
    except Exception:
        raise ConfirmationClientError("confirmation response unavailable or invalid; do not retry automatically") from None
    finally:
        try:
            connection.settimeout(previous_timeout)
        except Exception:
            pass
