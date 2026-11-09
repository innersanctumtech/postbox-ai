#!/usr/bin/env python3
"""Synthetic relay checks for reserved and uncertain handoffs."""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import handoff_fence  # noqa: E402
import relay  # noqa: E402


class Message:
    subject = "post.claude.project-a"
    metadata = types.SimpleNamespace(num_delivered=1, timestamp=None,
                                     sequence=types.SimpleNamespace(stream=17))


class Door:
    def __init__(self):
        self.deliveries = []

    def deliver(self, target, body):
        self.deliveries.append((target, body))


async def main():
    old_dir = os.environ.get("POSTBOX_SUPPORT_DIR")
    old_fence = relay._HANDOFF_FENCE
    os.environ["POSTBOX_SUPPORT_DIR"] = tempfile.mkdtemp(prefix="postbox-fence-")
    relay._HANDOFF_FENCE = None
    door, msg, target = Door(), Message(), object()
    checks = []
    try:
        first = await relay.fenced_deliver(msg, door, target, "fixture body", "synthetic-id")
        second = await relay.fenced_deliver(msg, door, target, "fixture body", "synthetic-id")
        checks.append(("an accepted handoff is not delivered twice", first and not second and len(door.deliveries) == 1))

        msg2 = Message()
        msg2.metadata = types.SimpleNamespace(num_delivered=1, timestamp=None,
                                              sequence=types.SimpleNamespace(stream=18))
        original_accept = handoff_fence.HandoffFence.accept
        def fail_accept(self, *args, **kwargs):
            raise OSError("synthetic acceptance failure")
        handoff_fence.HandoffFence.accept = fail_accept
        try:
            try:
                await relay.fenced_deliver(msg2, door, target, "fixture body", "synthetic-id-2")
            except OSError:
                checks.append(("acceptance failure leaves a reserved uncertain handoff", True))
            else:
                checks.append(("acceptance failure leaves a reserved uncertain handoff", False))
        finally:
            handoff_fence.HandoffFence.accept = original_accept
        try:
            await relay.fenced_deliver(msg2, door, target, "fixture body", "synthetic-id-2")
        except handoff_fence.FenceError:
            blocked = True
        else:
            blocked = False
        checks.append(("an uncertain handoff fails closed without a second delivery", blocked and len(door.deliveries) == 2))
    finally:
        relay._HANDOFF_FENCE = old_fence
        if old_dir is None:
            os.environ.pop("POSTBOX_SUPPORT_DIR", None)
        else:
            os.environ["POSTBOX_SUPPORT_DIR"] = old_dir
    failed = sum(not passed for _, passed in checks)
    for label, passed in checks:
        print(f"  {'ok' if passed else 'FAIL'}   {label}")
    print(f"\n{len(checks)-failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
