"""Check a short confirmation summary for obvious full-message disclosure."""

import re


def check_summary(summary, body):
    """Reject an exact normalized copy of the body; this is not semantic analysis."""
    if not isinstance(summary, str) or not isinstance(body, bytes):
        raise ValueError("summary and body have invalid types")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("message must be valid UTF-8") from exc
    text = re.sub(r"^\[postbox-[0-9a-f]{48}\]\n", "", text, count=1)
    normalized_body = " ".join(text.split())
    if normalized_body and normalized_body in " ".join(summary.split()):
        raise ValueError("summary includes the complete message")
