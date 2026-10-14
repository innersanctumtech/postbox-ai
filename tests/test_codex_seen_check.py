#!/usr/bin/env python3
"""Offline tests: after a Codex delivery the box checks the message really appeared in that conversation's log, and raises an alarm if not.
Run: .venv/bin/python tests/test_codex_seen_check.py

Why: a message can be reported `delivered` to a Codex screen while it sits in a conversation nobody has open.
`codex queue` says yes either way, so `delivered` alone proves nothing. Rollout files, the watch list and the clock are throwaway/faked.
"""
import os
import tempfile
os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["POSTBOX_MACHINE"] = "mac-a"
tmp = tempfile.mkdtemp(prefix="postbox-codex-seen-")
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(tmp, "codex-watch.json")
import json
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


ROLL = {"T1": os.path.join(tmp, "r1.jsonl"), "T2": os.path.join(tmp, "r2.jsonl")}
for p in ROLL.values():
    open(p, "w").write(json.dumps({"type": "session_meta", "payload": {"text": "old history mentions please run the tests"}}) + "\n")
lib.codex_thread_rollout = lambda tid: ROLL.get(tid)
BUSY = {}
lib.codex_busy_seconds = lambda tid, now=None: BUSY.get(tid)
S1 = lib.Session("codex", 11, "/a", "project-d", "T1", True, "folder", "", "", "recovery", "")
S2 = lib.Session("codex", 12, "/b", "project-a", "T2", True, "folder", "", "", "music", "")
T0 = 1_000_000.0
TEXT = "Please run the tests\nand report  the result line."


def append(tid, text):
    with open(ROLL[tid], "a") as f:
        f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "content": [{"type": "input_text", "text": text}]}}) + "\n")


print("-- (a) the message shows up in the conversation: seen, nothing raised")
lib.codex_watch_add(S1, TEXT, "claude@project-d", "d3", now=T0)
check("one message is being watched", len(lib.codex_watch_load()) == 1)
check("text already in the log BEFORE sending does not count", lib.codex_watch_check(now=T0 + 30) == [])
append("T1", "Message from claude@project-d#d3 (post box):\n\n" + TEXT + "\n\nIf this asks a question ...")
out = lib.codex_watch_check(now=T0 + 60)
check("found after it lands: reported seen", [r for _e, r in out] == ["seen"], out)
check("and no longer watched", lib.codex_watch_load() == [])

print("-- (b) never shows up while Codex is idle: unseen after the grace time")
lib.codex_watch_add(S2, "Second message for the music session", "claude@project-a", None, now=T0)
check("still inside the grace time: nothing yet", lib.codex_watch_check(now=T0 + lib.CODEX_SEEN_AFTER_S - 1) == [])
out = lib.codex_watch_check(now=T0 + lib.CODEX_SEEN_AFTER_S + 1)
check("past the grace time and idle: unseen", [r for _e, r in out] == ["unseen"], out)
e = out[0][0] if out else {}
check("the entry names who to tell", e.get("to") == S2.label and e.get("frm") == "claude@project-a", e)
check("and is dropped (raised once, not every round)", lib.codex_watch_load() == [])

print("-- (c) Codex mid-task: wait, up to the hard limit")
BUSY["T2"] = 120
lib.codex_watch_add(S2, "Third message while busy", "claude@project-a", None, now=T0)
check("busy past the grace time: still waiting", lib.codex_watch_check(now=T0 + lib.CODEX_SEEN_AFTER_S + 5) == [])
out = lib.codex_watch_check(now=T0 + lib.CODEX_SEEN_MAX_S + 1)
check("busy past the hard limit: unseen anyway", [r for _e, r in out] == ["unseen"], out)
BUSY.clear()

print("-- (d) the conversation's log cannot be found: unseen at once, never a silent pass")
S3 = lib.Session("codex", 13, "/c", "x", "GONE", True, "folder", "", "", "", "")
lib.codex_watch_add(S3, "Into the void", "claude@x", None, now=T0)
out = lib.codex_watch_check(now=T0 + 1)
check("no log file: unseen", [r for _e, r in out] == ["unseen"], out)

print("-- (e) a broken watch file never crashes the check")
open(os.environ["POSTBOX_CODEX_WATCH"], "w").write("{not json")
check("broken file reads as empty", lib.codex_watch_load() == [] and lib.codex_watch_check(now=T0) == [])

print("-- (f) relay: an unseen message tells the sender and runs the alarm script; a seen one does neither")
import asyncio  # noqa: E402
import relay  # noqa: E402
hits = os.path.join(tmp, "alarm-hits.txt")
fake = os.path.join(tmp, "fake-alarm.sh")
open(fake, "w").write(f'#!/bin/bash\necho "$1|$2|$3" >> {hits}\n')
relay.ALARM_CMD = fake
told = []


async def fake_notify(nc, doors, frm, from_session, notice):
    told.append((frm, from_session, notice))
    return "sent"


relay.notify_sender = fake_notify
lib.codex_watch_add(S2, "Never arrives at all", "claude@project-a", "mix", now=T0)
out = asyncio.run(relay.check_codex_seen(None))
check("the relay settles it as unseen", [r for _e, r in out] == ["unseen"], out)
check("the sender is told, in its own session", told and told[0][0] == "claude@project-a" and told[0][1] == "mix" and "NOT appeared" in told[0][2], told)
alarm = open(hits).read() if os.path.exists(hits) else ""
check("the alarm script ran with a spoken title naming the Mac", "postbox-codex-unseen|Mac-a post box: Codex missed a message|" in alarm, alarm)
told.clear()
open(hits, "w").close()
lib.codex_watch_add(S1, "This one lands fine", "claude@project-d", None, now=__import__("time").time())
append("T1", "Message from claude@project-d (post box):\n\nThis one lands fine")
out = asyncio.run(relay.check_codex_seen(None))
check("a seen message raises nothing", [r for _e, r in out] == ["seen"] and not told and open(hits).read() == "", (out, told))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
