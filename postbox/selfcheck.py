#!/usr/bin/env python3
"""Self-check: one real round trip through a post box, no agent session needed.

It publishes a message for an agent name that has no delivery door and expects the relay to answer
"refused ... no delivery door". That single answer proves the mailbox is reachable, our login is accepted, its stream exists and
the relay is running and consuming. Nothing stays behind: a refused message is dropped, not stored.

  selfcheck.py                  this Mac's post box (POSTBOX_NATS_URL picks a mailbox other than the default)
  selfcheck.py --peer NAME      the post box of another linked Mac, through its own relay (proves the network path, that our
                                key is accepted there, and that its relay is alive). --peer=NAME works too.

This Mac's own mailbox is checked only when --peer is not given at all. A bare --peer, an empty name or any other argument is an error
(exit 2, with the usage), never a quiet fall back to a different check that would print "selfcheck ok" for a link nobody tested.

Exit 0 = the whole path works. Exit 1 = it does not. Exit 2 = the command line is wrong. Exit 3 = the mailbox answered but rejected
our key (a different problem from "down"). The reason is on stderr in plain words.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import time  # noqa: E402

import lib  # noqa: E402

LAST = {}   # what the last run() found out, for its one telemetry event (a fixed class, never the error text)


def fail(reason, code=1, cls="other"):
    LAST["cls"] = cls
    print(f"selfcheck FAILED: {reason}", file=sys.stderr)
    return code


def parse_args(argv=None):
    """--peer NAME (or --peer=NAME) picks another linked Mac. Anything else, including a bare --peer, exits 2 with the usage."""
    ap = argparse.ArgumentParser(prog="selfcheck.py", allow_abbrev=False,
                                 description="One real round trip through a post box. Without --peer it checks this Mac's own post box.")
    ap.add_argument("--peer", metavar="NAME", help="check the post box of this other linked Mac instead (for example --peer mac-b)")
    args = ap.parse_args(argv)
    if args.peer is not None and lib.norm(args.peer) == "unknown":
        ap.error("--peer needs the name of another linked Mac, for example --peer mac-b")
    return args


async def run(peer=None):
    """The check, plus one post_link event for a check of ANOTHER Mac (op=selfcheck): the post box's own record of whether that Mac answered."""
    started = time.monotonic()
    LAST.clear()
    code = await _run(peer)
    if peer is not None:
        outcome = "ok" if code == 0 else ("refused" if code == 3 else ("setup" if LAST.get("cls") == "setup" else "unreachable"))
        lib.telemetry.emit("post_link", "selfcheck", status="success" if code == 0 else "error", duration_ms=lib.telemetry.elapsed_ms(started),
                           peer=lib.norm(peer), op="selfcheck", outcome=outcome, error_class=None if code == 0 else LAST.get("cls", "other"),
                           direction="out", exit_code=code)
    return code


async def _run(peer=None):
    from nats.errors import TimeoutError as NatsTimeout

    remote = peer is not None
    try:
        target = lib.peer_target(peer) if remote else lib.local_target()
        nc = await lib.connect_fast(target, timeout=3)
    except lib.MailboxError as exc:
        cls = lib.telemetry.connect_error_class(exc)
        if exc.setup:
            return fail(str(exc), cls="setup")
        if exc.refused:
            return fail(f"the mailbox of {peer if remote else 'this Mac'} rejected this Mac's login key: {exc}", 3, cls=cls)
        return fail(f"cannot reach the mailbox of {peer} ({exc})" if remote else f"cannot reach the mailbox at {lib.NATS_URL}: {exc}", cls=cls)
    try:
        inbox = nc.new_inbox()
        sub = await nc.subscribe(inbox)
        sender = "installer@selfcheck" + (f"@{lib.my_machine()}" if remote else "")
        headers = {"From": sender, "Reply-Inbox": inbox, "Nats-Msg-Id": uuid.uuid4().hex}
        if not remote:
            headers["From-Pid"] = "0"
        try:
            await nc.jetstream().publish("post.selfcheck.selfcheck", b"self-check, please ignore", headers=headers, timeout=5)
        except Exception as exc:  # noqa: BLE001
            return fail(f"the mailbox did not accept a message (has the relay ever created the stream?): {exc!r}", cls="publish_failed")
        try:
            outcome = json.loads((await sub.next_msg(timeout=8)).data)
        except NatsTimeout:
            return fail("the mailbox stored the message but the relay never answered (is the relay running?)", cls="relay_silent")
        if outcome.get("status") == "refused" and "no delivery door" in outcome.get("detail", ""):
            print(f"selfcheck ok: the mailbox{' of ' + peer if remote else ''}, its stream and the relay all answered")
            return 0
        return fail(f"the relay gave an unexpected answer: {outcome}", cls="bad_answer")
    finally:
        await nc.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args().peer)))
