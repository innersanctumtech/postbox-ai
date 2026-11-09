#!/usr/bin/env python3
"""Synthetic ordering check for stable delivery correlation and broker settlement."""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import relay  # noqa: E402


async def main():
    events = []

    class Message:
        async def ack(self):
            events.append("ack")

    original = relay.log
    relay.log = lambda event, **fields: events.append((event, fields))
    try:
        await relay.ack_delivery(Message(), "synthetic-id", "claude@project-a", "codex@project-a", ["codex:thread-a"])
    finally:
        relay.log = original
    passed = (events[0][0] == "delivery_correlation"
              and events[0][1]["id"] == "synthetic-id"
              and events[1] == "ack")
    print(f"  {'ok' if passed else 'FAIL'}   delivery correlation is logged before acknowledgement")
    print(f"\n{int(passed)} passed, {int(not passed)} failed")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
