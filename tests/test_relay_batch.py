#!/usr/bin/env python3
"""Synthetic relay batch regression: one failed message must not starve later mail."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["POSTBOX_SUPPORT_DIR"] = tempfile.mkdtemp(prefix="postbox-batch-")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import relay  # noqa: E402


class Message:
    def __init__(self, name): self.name, self.nak_delay = name, None
    async def nak(self, delay=None): self.nak_delay = delay


old, later, handled = Message("old"), Message("later"), []

async def fake_handle(_nc, message):
    if message is old:
        raise RuntimeError("old message cannot deliver")
    handled.append(message.name)


with mock.patch.object(relay, "handle", fake_handle):
    asyncio.run(relay.handle_batch(object(), [old, later]))

ok = old.nak_delay == 10 and handled == ["later"]
print("1 passed, 0 failed" if ok else f"0 passed, 1 failed: {old.nak_delay!r}, {handled!r}")
sys.exit(0 if ok else 1)
