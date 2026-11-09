#!/usr/bin/env python3
"""Synthetic checks for relay maintenance scheduling and fetch timeout handling."""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import relay  # noqa: E402


async def check_maintenance_runs_beside_delivery():
    entered = asyncio.Event()
    release = asyncio.Event()
    observed = []

    async def maintenance():
        entered.set()
        await release.wait()

    started = relay.start_maintenance("synthetic", maintenance())
    await entered.wait()
    observed.append("delivery progressed")
    release.set()
    await relay.drain_maintenance(timeout=1)
    return started and observed == ["delivery progressed"] and not relay.maintenance_running()


async def check_fetch_timeout_classification():
    return relay.is_fetch_timeout(TimeoutError()) and relay.is_fetch_timeout(asyncio.TimeoutError()) and not relay.is_fetch_timeout(ValueError())


async def main():
    checks = [
        ("maintenance is scheduled without blocking delivery", await check_maintenance_runs_beside_delivery()),
        ("plain and asyncio timeouts mean an empty fetch", await check_fetch_timeout_classification()),
    ]
    for label, passed in checks:
        print(f"  {'ok' if passed else 'FAIL'}   {label}")
    failed = sum(not passed for _, passed in checks)
    print(f"\n{len(checks) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
