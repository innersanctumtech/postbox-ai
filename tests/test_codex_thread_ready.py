#!/usr/bin/env python3
"""Offline tests: a Codex screen started with plain `codex` is a delivery target only once its OWN conversation exists.
Run: .venv/bin/python tests/test_codex_thread_ready.py

The thread table is a throwaway sqlite file and process start times are faked: nothing here touches ~/.codex or a real process.
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
os.environ["POSTBOX_MACHINE"] = "mac-a"
import lib  # noqa: E402
import relay  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


NOW = time.time()
OLD, LIVE = "01a0f97e-0000-7000-8000-000000000001", "01a0fdee-0000-7000-8000-000000000002"
tmp = tempfile.mkdtemp(prefix="postbox-codex-ready-")
DB = os.path.join(tmp, "state_5.sqlite")
con = sqlite3.connect(DB)
con.execute("create table threads (id text primary key, cwd text, archived integer, updated_at integer)")
con.commit()
lib.codex_state_db = lambda: DB


def set_thread(tid, cwd, updated, archived=0):
    con.execute("insert or replace into threads values (?,?,?,?)", (tid, cwd, archived, int(updated)))
    con.commit()


def clear():
    con.execute("delete from threads")
    con.commit()


AGE = {57635: 600.0}                       # the screen opened 10 minutes ago
lib.process_age_s = lambda pid: AGE.get(pid)
lib.cwd_of = lambda pid: "/home/u/.codex"
STARTED = NOW - 600
PLAIN = {57635: (1, ["node", "/x/bin/codex"])}                       # no thread id in its arguments
RESUMED = {57635: (1, ["node", "/x/bin/codex", "resume", OLD])}      # explicit thread id


def sessions(table=PLAIN):
    return lib.codex_sessions(table)


print("-- (a) newest thread older than the screen: not ready")
clear()
set_thread(OLD, "/home/u/.codex", STARTED - 3600)
s = sessions()
check("the screen is still listed (post who shows it) but is not ready and has no thread", len(s) == 1 and not s[0].ready and s[0].target == "" and s[0].project == "codex")
door = lib.CodexDoor()
check("resolve() returns no targets for it", door.resolve(".codex", sessions=s) == [])
check("waiting() names it", [w.pid for w in door.waiting(".codex", sessions=s)] == [57635])


class Msg:
    def __init__(self, delivered=1):
        self.subject, self.data = "post.codex.codex", b"hello"
        self.headers = {"From": "claude@myproj", "From-Pid": "1", "Reply-Inbox": "_INBOX.x"}
        self.metadata = types.SimpleNamespace(num_delivered=delivered, sequence=types.SimpleNamespace(stream=1, consumer=1))
        self.acked = self.naked = self.termed = None

    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): self.termed = True


class NC:
    def __init__(self): self.sent = []
    async def publish(self, subj, data): self.sent.append((subj, json.loads(data)))
    async def flush(self, timeout=None): pass


class SessionDoor(lib.CodexDoor):
    """The real door, fed a fixed session list instead of the process table; deliver() records instead of running `codex`."""
    def __init__(self, found): self.found, self.handed = found, []
    def resolve(self, project, exclude_pid=None, sessions=None): return super().resolve(project, exclude_pid, self.found())
    def waiting(self, project, exclude_pid=None, sessions=None): return super().waiting(project, exclude_pid, self.found())
    def busy_note(self, session, now=None): return ""
    def deliver(self, session, text, timeout=25.0): self.handed.append((session.target, text))


def handle(door, delivered=1):
    nc, m, events = NC(), Msg(delivered), []
    with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(relay.tel, "emit", lambda *a, **k: events.append((a, k))):
        asyncio.run(relay.handle(nc, m, {"codex": door}, lib.PairLimiter(), lib.PairLimiter(limit=100)))
    return nc, m, events


door = SessionDoor(sessions)
nc, m, events = handle(door)
answer = nc.sent[0][1]
check("handle() answers queued with reason thread_not_ready, in plain words",
      answer["status"] == "queued" and answer["reason"] == "thread_not_ready" and "has not started a conversation yet" in answer["detail"]
      and "kept for 7 days" in answer["detail"] and "screen in codex has" in answer["detail"], answer)
check("the mail is kept (nak 10 s), not acked, and nothing was handed to any thread", m.naked == 10 and not m.acked and door.handed == [])
check("telemetry carries reason thread_not_ready and no message body",
      any(k.get("reason") == "thread_not_ready" and k.get("result") == "queued" for _a, k in events) and "hello" not in json.dumps(events, default=str))
nc, m, _ = handle(door, delivered=3)
check("a retry is silent (no second reply) and still kept", nc.sent == [] and m.naked == 10)

print("-- (b) newest thread updated after the screen started: delivers")
clear()
set_thread(LIVE, "/home/u/.codex", STARTED + 30)
s = sessions()
check("it resolves to that thread and is ready", len(s) == 1 and s[0].ready and s[0].target == LIVE)
door = SessionDoor(sessions)
nc, m, _ = handle(door)
check("handle() delivers into that thread", nc.sent[0][1]["status"] == "delivered" and door.handed and door.handed[0][0] == LIVE and m.acked)

print("-- (c) an explicit thread id is resolved by id, however old")
clear()
set_thread(OLD, "/home/u/.codex", STARTED - 86400)
s = sessions(RESUMED)
check("a resumed screen delivers to its thread with no start-time check", len(s) == 1 and s[0].ready and s[0].target == OLD)

print("-- (d) the slack boundary (default 5 s)")
for label, offset, ready in (("3 s before the start is inside the slack", -3, True), ("4 s before is inside (the clock moves a little while the test runs)", -4, True),
                             ("7 s before is outside", -7, False)):
    clear()
    set_thread(OLD, "/home/u/.codex", time.time() - 600 + offset)   # measured from NOW, not from the import: a slow run must not eat the 1 s margin of the -4 case
    set_thread("older-thread", "/home/u/.codex", STARTED - 99999)   # an older thread in the same folder never matters
    s = sessions()
    check(label, len(s) == 1 and s[0].ready is ready, s)
check("the slack is set by POSTBOX_CODEX_THREAD_SLACK_S (read at import)", lib.CODEX_THREAD_SLACK_S == float(os.environ.get("POSTBOX_CODEX_THREAD_SLACK_S", "5")))

print("-- (e) the screen becomes deliverable when its thread row is written")
clear()
set_thread(OLD, "/home/u/.codex", STARTED - 3600)
door = SessionDoor(sessions)
check("before: no targets", door.resolve(".codex") == [] and len(door.waiting(".codex")) == 1)
set_thread(LIVE, "/home/u/.codex", STARTED + 2)
check("after the new thread row exists: one target, its own thread", [t.target for t in door.resolve(".codex")] == [LIVE] and door.waiting(".codex") == [])
clear()
set_thread(OLD, "/home/u/.codex", STARTED - 3600)
set_thread(OLD, "/home/u/.codex", NOW)   # the user resumed the old conversation and prompted: updated after the start
check("resuming an old thread and prompting also makes it ready", [t.target for t in SessionDoor(sessions).resolve(".codex")] == [OLD])

print("-- screen with no conversation in its folder at all")
clear()
s = sessions()
check("listed as not ready (no thread to hand mail to)", len(s) == 1 and not s[0].ready and s[0].target == "")

print("-- ps failures fail open")
clear()
set_thread(OLD, "/home/u/.codex", STARTED - 3600)
AGE.clear()
check("when the start time cannot be read the old behaviour stays (ready)", sessions()[0].ready)
AGE[57635] = 600.0

print("-- (f) no codex screen at all: still nobody_there")
door = SessionDoor(lambda: [])
nc, m, events = handle(door)
check("answer is queued with reason nobody_there and the old wording",
      nc.sent[0][1]["reason"] == "nobody_there" and "nobody reachable" in nc.sent[0][1]["detail"] and m.naked == 10)
check("telemetry reason is nobody_there", any(k.get("reason") == "nobody_there" for _a, k in events))
other = lib.Session("codex", 1, "/x/other", "other", "t")
nc, m, _ = handle(SessionDoor(lambda: [other]))
check("a screen in ANOTHER project does not turn nobody_there into thread_not_ready", nc.sent[0][1]["reason"] == "nobody_there")

print("-- a redelivery of waiting mail writes no log line (one line per message, not one per 10 s)")
def handle_out(door, delivered):
    nc, m, buf = NC(), Msg(delivered), io.StringIO()
    with contextlib.redirect_stdout(buf), mock.patch.object(relay.tel, "emit", lambda *a, **k: None):
        asyncio.run(relay.handle(nc, m, {"codex": door}, lib.PairLimiter(), lib.PairLimiter(limit=100)))
    return buf.getvalue(), m
out1, _m1 = handle_out(SessionDoor(lambda: []), 1)
out3, m3 = handle_out(SessionDoor(lambda: []), 3)
check("the first try logs nobody_there", '"nobody_there"' in out1)
check("a redelivery (third try) logs nothing but still keeps the mail (nak 10 s)", '"nobody_there"' not in out3 and m3.naked == 10 and not m3.acked)
t1, _ = handle_out(SessionDoor(sessions), 1)
t4, m4 = handle_out(SessionDoor(sessions), 4)
check("thread_not_ready: first try logs, a redelivery does not, the mail is kept", '"thread_not_ready"' in t1 and '"thread_not_ready"' not in t4 and m4.naked == 10)

print("-- process_age_s parses ps etime")
import importlib  # noqa: E402
importlib.reload(lib)
for text, secs in (("05:30", 330), ("01:02:03", 3723), ("2-03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5), ("", None), ("junk", None)):
    with mock.patch.object(lib, "_run", lambda *a, **k: text + "\n"):
        check(f"etime {text!r}", lib.process_age_s(1) == secs)
check("a real process has an age", 0 <= (lib.process_age_s(os.getpid()) if lib.process_age_s(os.getpid()) is not None else -1) < 3600)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
