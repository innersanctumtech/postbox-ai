#!/usr/bin/env python3
"""Synthetic checks for the exclusive local relay lease."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
from relay_lease import LeaseError, RelayLease  # noqa: E402


temporary = tempfile.TemporaryDirectory(prefix="postbox-lease-test-", dir=str(ROOT))
base = Path(temporary.name)
base.chmod(0o700)
lease_path = base / "relay.lease"
first = RelayLease.acquire(lease_path)
first.assert_held()

try:
    RelayLease.acquire(lease_path)
except LeaseError:
    exclusive = True
else:
    exclusive = False
print("ok   only one local process can hold a lease" if exclusive else "FAIL second lease was acquired")

first.close()
second = RelayLease.acquire(lease_path)
second.assert_held()
second.close()
print("ok   closing releases the lease for a later owner")

try:
    RelayLease.acquire(Path("relative.lease"))
except LeaseError:
    absolute_required = True
else:
    absolute_required = False
print("ok   relative lease paths are refused" if absolute_required else "FAIL relative lease path accepted")

temporary.cleanup()
sys.exit(0 if exclusive and absolute_required else 1)
