#!/usr/bin/env python3
"""Synthetic ordering check for stable delivery correlation and broker settlement."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import relay  # noqa: E402
import delivery_status  # noqa: E402


async def main():
    events = []

    class Message:
        async def ack(self):
            events.append("ack")

    original = relay.log
    relay.log = lambda event, **fields: events.append((event, fields))
    try:
        await relay.ack_delivery(Message(), "synthetic-id", "claude@project-a", "codex@project-a", ["thread-a"])
    finally:
        relay.log = original
    passed = (events[0][0] == "delivery_correlation"
              and events[0][1]["message_id"] == "synthetic-id"
              and len(events[0][1]["delivery_ids"]) == 1
              and events[1] == "ack")
    descriptor, name = tempfile.mkstemp(prefix="postbox-status-")
    os.close(descriptor)
    path = Path(name)
    try:
        path.write_text(json.dumps(events[0][1] | {"event": "delivery_correlation"}) + "\n")
        handoff = delivery_status.lookup(path, "synthetic-id")
        path.write_text(path.read_text() + json.dumps({"event": "recipient_seen", "id": events[0][1]["delivery_ids"][0]}) + "\n")
        appeared = delivery_status.lookup(path, "synthetic-id")
        passed = passed and handoff["state"] == "handoff" and appeared["state"] == "appeared"
    finally:
        path.unlink(missing_ok=True)
    print(f"  {'ok' if passed else 'FAIL'}   delivery correlation is logged before acknowledgement")
    print(f"\n{int(passed)} passed, {int(not passed)} failed")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
