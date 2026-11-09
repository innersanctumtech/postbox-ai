#!/usr/bin/env python3
"""Synthetic check that a watch recovery pass shares one process discovery."""
import asyncio
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import lib  # noqa: E402
import relay  # noqa: E402


async def main():
    calls = {"discovery": 0, "deliver": [], "requeue": []}
    first = lib.Session("codex", 41, "/tmp/project-a", "project-a", "thread-a", True)
    second = lib.Session("codex", 42, "/tmp/project-b", "project-b", "thread-b", True)

    class Door(lib.CodexDoor):
        def deliver(self, session, body):
            calls["deliver"].append(session.target)

    door = Door()
    entries = [
        ({"id": "m1", "thread": "thread-a", "project": "project-a", "body": "fixture-a", "sent": 1}, "retry"),
        ({"id": "m2", "thread": "thread-b", "project": "project-b", "body": "fixture-b", "sent": 1}, "recover"),
    ]
    old_watch = lib.codex_watch_check
    old_discovery = lib.codex_discovery_snapshot
    old_requeue = lib.codex_watch_requeue
    try:
        lib.codex_watch_check = lambda: entries
        def discovery():
            calls["discovery"] += 1
            return [first, second], [{"pid": 41, "start": "a"}, {"pid": 42, "start": "b"}]
        lib.codex_discovery_snapshot = discovery
        lib.codex_watch_requeue = lambda mid, session: calls["requeue"].append((mid, session.target))
        await relay.check_codex_seen(object(), {"codex": door})
    finally:
        lib.codex_watch_check = old_watch
        lib.codex_discovery_snapshot = old_discovery
        lib.codex_watch_requeue = old_requeue
    passed = calls["discovery"] == 1 and calls["deliver"] == ["thread-a", "thread-b"] and len(calls["requeue"]) == 2
    print(f"  {'ok' if passed else 'FAIL'}   one native screen snapshot is reused across watch recoveries")
    print(f"\n{int(passed)} passed, {int(not passed)} failed")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
