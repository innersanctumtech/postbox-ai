#!/usr/bin/env python3
"""Offline tests for the post box logic. Run: .venv/bin/python tests/test_postbox.py"""
import asyncio
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool (tests/test_telemetry.py covers telemetry itself)
import socket
import sys
import tempfile
import threading
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
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


def raises(label, fn, kind=ValueError, contains=""):
    """Passes only for the expected kind of error (and, when given, text in its message): an unrelated crash is a failure, not a pass."""
    try:
        fn()
    except kind as exc:
        check(label, contains in str(exc), f"(wrong message: {exc})")
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"(unexpected {type(exc).__name__}: {exc})")
    else:
        check(label, False, "(no error raised)")


# ---- addresses
check("norm strips dots and case", lib.norm(".Claude") == "claude" and lib.norm("Project D") == "project-d")
check("bare agent uses current project", lib.parse_address("claude", "Downloads") == ("claude", "Downloads"))
check("agent@project", lib.parse_address("codex@project-b", "x") == ("codex", "project-b"))
raises("empty address rejected", lambda: lib.parse_address("", "x"), ValueError, "bad address")
raises("missing agent rejected", lambda: lib.parse_address("@proj", "x"), ValueError, "bad address")

# ---- resolving the receiving session
S = lib.Session
sessions = [S("claude", 1, "/a/Downloads", "Downloads", "/s/1"), S("claude", 2, "/a/Downloads", "Downloads", "/s/2"),
            S("claude", 3, "/a/python", "python", "/s/3")]
door = lib.ClaudeDoor()
check("same project, sender excluded", [s.pid for s in door.resolve("downloads", exclude_pid=1, sessions=sessions)] == [2])
check("other project untouched", [s.pid for s in door.resolve("python", exclude_pid=1, sessions=sessions)] == [3])
check("nobody in an unknown project", door.resolve("nope", sessions=sessions) == [])

# ---- message shape
frame = json.loads(lib.claude_frame("hi there"))
check("frame is a user message", frame["type"] == "user" and frame["message"]["content"] == "hi there")
body = lib.render("claude@Downloads", "run the tests")
check("receiver told how to answer, not to reply to plain information, and not to poll",
      'post claude@Downloads "your reply"' in body and "only informs you, do not reply" in body and "Never wait or poll" in body and "RUNNING this shell command" in body)

# ---- stop rule
lim = lib.PairLimiter(limit=3, window=60)
check("first 3 allowed", all(lim.allow(("a", "b"), now=t) for t in (0, 1, 2)))
check("4th blocked", not lim.allow(("a", "b"), now=3))
check("other pair unaffected", lim.allow(("a", "c"), now=3))
check("allowed again after the window", lim.allow(("a", "b"), now=100))

# ---- real socket delivery, happy path and dead socket
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "9.sock")
got = []
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(path)
srv.listen(1)


def accept():
    c, _ = srv.accept()
    got.append(c.recv(65536))
    c.close()


threading.Thread(target=accept, daemon=True).start()
door.deliver(S("claude", 9, tmp, "t", path), "over the socket")
threading.Event().wait(0.3)
check("frame arrives on the socket", got and json.loads(got[0])["message"]["content"].startswith("over the socket\n\n"))
check("Claude delivery ends with the real-sender note", got and json.loads(got[0])["message"]["content"].endswith(lib.SENDER_NOTE)
      and '"Message from" line' in lib.SENDER_NOTE and "another Claude session" in lib.SENDER_NOTE)
raises("dead socket raises", lambda: door.deliver(S("claude", 10, tmp, "t", os.path.join(tmp, "none.sock")), "x"), FileNotFoundError)


# ---- relay decisions with fakes
class FakeMsg:
    def __init__(self, subject, data, headers, delivered=1):
        self.subject, self.data, self.headers = subject, data.encode(), headers
        self.metadata = types.SimpleNamespace(num_delivered=delivered)
        self.acked = self.naked = self.termed = None

    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): self.termed = True


class FakeNC:
    def __init__(self): self.sent = []
    async def publish(self, subj, data): self.sent.append((subj, json.loads(data)))
    async def flush(self, timeout=None): pass


class FakeDoor:
    def __init__(self, targets, fail=False): self.targets, self.fail, self.delivered = targets, fail, []
    def resolve(self, project, exclude_pid=None): return self.targets
    def deliver(self, t, text):
        if self.fail:
            raise OSError("refused")
        self.delivered.append((t.pid, text))


HDR = {"From": "claude@Downloads", "From-Pid": "1", "Reply-Inbox": "_INBOX.x"}
run = asyncio.run

m, nc, d = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC(), FakeDoor([sessions[1]])
run(relay.handle(nc, m, {"claude": d}, lib.PairLimiter()))
check("delivered: acked, receiver got text, sender told", m.acked and "hello" in d.delivered[0][1] and nc.sent[0][1]["status"] == "delivered")

m, nc = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC()
run(relay.handle(nc, m, {"claude": FakeDoor([])}, lib.PairLimiter()))
check("nobody there: kept (nak), sender told queued once", m.naked == 10 and not m.acked and nc.sent[0][1]["status"] == "queued")

m, nc = FakeMsg("post.downloads.claude", "hello", HDR, delivered=3), FakeNC()
run(relay.handle(nc, m, {"claude": FakeDoor([])}, lib.PairLimiter()))
check("retry rounds stay quiet", m.naked == 10 and nc.sent == [])

m, nc = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC()
run(relay.handle(nc, m, {"claude": FakeDoor([sessions[1]], fail=True)}, lib.PairLimiter()))
check("door failure: kept for retry, not acked", m.naked == 10 and not m.acked)

m, nc = FakeMsg("post.downloads.grok", "hello", HDR), FakeNC()
run(relay.handle(nc, m, {"claude": FakeDoor([])}, lib.PairLimiter()))
check("unknown agent: refused loudly", m.termed and nc.sent[0][1]["status"] == "refused")

m, nc = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC()
run(relay.handle(nc, m, {"claude": FakeDoor([sessions[1]])}, lib.PairLimiter(limit=0)))
check("stop rule: refused", m.termed and nc.sent[0][1]["status"] == "refused" and "stop rule" in nc.sent[0][1]["detail"])

m, nc = FakeMsg("garbage", "x", HDR), FakeNC()
run(relay.handle(nc, m, {}, lib.PairLimiter()))
check("bad subject dropped", m.termed)

# ---- Codex: which processes are screens, thread lookup, door, sender recognition
U = "01a00000-0000-7000-8000-000000000a03"
check("plain codex is a screen", lib.codex_screen_args([]) == (True, None))
check("resume with id is a screen and carries the thread", lib.codex_screen_args(["--dangerously-bypass-approvals-and-sandbox", "resume", U]) == (True, U))
check("exec is not a screen", lib.codex_screen_args(["exec", "--json", "x"]) == (False, None))
check("app-server is not a screen", lib.codex_screen_args(["app-server", "--listen", "unix://"]) == (False, None))
check("flag values are skipped, a prompt is still a screen", lib.codex_screen_args(["-C", "/tmp/x", "fix the bug"]) == (True, None))
check("a flag value that looks like a subcommand is still a screen", lib.codex_screen_args(["-C", "exec"]) == (True, None))

import sqlite3
db = os.path.join(tmp, "state_9.sqlite")
con = sqlite3.connect(db)
con.execute("create table threads (id text, cwd text, archived integer, updated_at integer)")
con.executemany("insert into threads values (?,?,?,?)", [("t-old", "/p/a", 0, 1), ("t-new", "/p/a", 0, 5), ("t-arch", "/p/a", 1, 9), ("t-b", "/p/b", 0, 2)])
con.commit()
con.close()
lib.codex_state_db = lambda: db
check("newest live thread in a folder wins, archived ignored", lib.codex_thread_info(cwd="/p/a") == ("t-new", "/p/a"))
check("lookup by thread id", lib.codex_thread_info(thread_id="t-b") == ("t-b", "/p/b"))
check("unknown folder gives nothing", lib.codex_thread_info(cwd="/nope") is None)

cs = [S("codex", 10, "/p/a", "a", "t-new"), S("codex", 11, "/p/a", "a", "t-other"), S("codex", 12, "/p/b", "b", "t-b")]
cdoor = lib.CodexDoor()
check("codex resolve: same project minus sender", [s.pid for s in cdoor.resolve("a", exclude_pid=10, sessions=cs)] == [11])
check("codex resolve: other project", [s.pid for s in cdoor.resolve("b", sessions=cs)] == [12])

bindir = os.path.join(tmp, "bin")
os.makedirs(bindir)
fake = os.path.join(bindir, "codex")
open(fake, "w").write('#!/bin/bash\necho "$@" > "%s/args.txt"\n[ -n "$FAKE_CODEX_FAIL" ] && { echo boom >&2; exit 1; }\nexit 0\n' % tmp)
os.chmod(fake, 0o755)
old_path = os.environ["PATH"]
os.environ["PATH"] = bindir + ":" + old_path
cdoor.deliver(S("codex", 10, "/p/a", "a", "t-new"), "hello codex")
check("codex door runs `codex queue --thread <id> --message <text>`", open(os.path.join(tmp, "args.txt")).read().strip() == "queue --thread t-new --message hello codex")
os.environ["FAKE_CODEX_FAIL"] = "1"
raises("codex door failure raises", lambda: cdoor.deliver(S("codex", 10, "/p/a", "a", "t-new"), "x"), RuntimeError, "boom")
del os.environ["FAKE_CODEX_FAIL"]
os.environ["PATH"] = old_path

me = os.getpid()
fake_table = {me: (500, ["python3"]), 500: (501, ["bash"]), 501: (10, ["codex-native"]), 10: (1, ["node", "/x/bin/codex", "resume", "t-new"])}
lib.cwd_of = lambda pid: "/p/a"
lib.codex_thread_info = lambda thread_id=None, cwd=None: ("t-new", "/p/a")
anc = lib.codex_ancestor(fake_table)
check("a process inside a codex screen is recognised as that screen", anc is not None and anc.pid == 10 and anc.project == "a")
check("a process outside any codex screen is not", lib.codex_ancestor({me: (1, ["python3"])}) is None)

# ---- Codex busy note: a screen reads its inbox only between tasks, so the sender is told when it is mid-task
from datetime import datetime, timezone  # noqa: E402

rdb = os.path.join(tmp, "state_10.sqlite")
lib.codex_state_db = lambda: rdb
BEGAN = datetime(2026, 9, 30, 13, 28, 37, 802000, tzinfo=timezone.utc).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def marker(kind, at=BEGAN, started_at=None):
    payload = {"type": kind, "turn_id": "t"}
    if started_at is not None:
        payload["started_at"] = started_at
    return json.dumps({"timestamp": iso(at), "ordinal": 1, "type": "event_msg", "payload": payload}, separators=(",", ":"))


def filler(n=200):
    return json.dumps({"timestamp": iso(BEGAN), "type": "response_item", "payload": {"type": "reasoning", "text": "x" * n}}, separators=(",", ":"))


_threads = {}


def thread(tid, lines, missing_file=False, null_path=False):
    path = os.path.join(tmp, f"rollout-{tid}.jsonl")
    if not missing_file:
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
    c = sqlite3.connect(rdb)
    c.execute("create table if not exists threads (id text, rollout_path text, cwd text, archived integer, updated_at integer)")
    c.execute("insert into threads values (?,?,?,?,?)", (tid, None if null_path else path, "/p/x", 0, 1))
    c.commit()
    c.close()
    return path


thread("b-busy", [filler(), marker("task_started")])
check("mid-task: seconds since the task started", lib.codex_busy_seconds("b-busy", now=BEGAN + 240) == 240)
thread("b-startedat", [filler(), marker("task_started", started_at=BEGAN - 100)])
check("the task's own started_at wins over the line's timestamp", lib.codex_busy_seconds("b-startedat", now=BEGAN + 240) == 340)
thread("b-done", [marker("task_started"), filler(), marker("task_complete", BEGAN + 30)])
check("finished task: idle (nothing said)", lib.codex_busy_seconds("b-done", now=BEGAN + 240) is None)
thread("b-second", [marker("task_started"), marker("task_complete", BEGAN + 30), filler(), marker("task_started", BEGAN + 600)])
check("a second task after a finished one: busy since the second start", lib.codex_busy_seconds("b-second", now=BEGAN + 700) == 100)
thread("b-aborted", [marker("task_started"), filler(), marker("turn_aborted", BEGAN + 20)])
check("interrupted task: idle", lib.codex_busy_seconds("b-aborted", now=BEGAN + 240) is None)
thread("b-none", [filler(), filler()])
check("no marker at all: unknown, so nothing is claimed", lib.codex_busy_seconds("b-none", now=BEGAN + 240) is None)
check("unknown thread: nothing claimed", lib.codex_busy_seconds("no-such-thread", now=BEGAN) is None)
thread("b-nofile", [], missing_file=True)
check("log file gone: nothing claimed", lib.codex_busy_seconds("b-nofile", now=BEGAN) is None)
thread("b-nullpath", [], null_path=True)
check("thread row without a log path: nothing claimed", lib.codex_busy_seconds("b-nullpath", now=BEGAN) is None)
thread("b-quoted", [marker("task_complete"), json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": marker("task_started")}}, separators=(",", ":"))])
check("a marker quoted inside a tool output is not a marker", lib.codex_busy_seconds("b-quoted", now=BEGAN + 60) is None)

# reading the log backwards in chunks: a marker line cut by a chunk edge, a marker far back, and the give-up cap
old_chunk, old_max = lib.CODEX_TAIL_CHUNK, lib.CODEX_TAIL_MAX
lib.CODEX_TAIL_CHUNK = 256
edge_ok = True
for pad in range(0, 60, 7):
    thread(f"b-edge{pad}", [filler(pad + 20), marker("task_started")] + [filler(90)] * 6)
    thread(f"b-edgedone{pad}", [marker("task_started"), marker("task_complete", BEGAN + 5)] + [filler(90)] * 6)
    edge_ok = edge_ok and lib.codex_busy_seconds(f"b-edge{pad}", now=BEGAN + 60) == 60 and lib.codex_busy_seconds(f"b-edgedone{pad}", now=BEGAN + 60) is None
check("marker found wherever a chunk edge falls (busy and idle)", edge_ok)
thread("b-far", [marker("task_started")] + [filler(280)] * 40)
check("marker many chunks back is still found", lib.codex_busy_seconds("b-far", now=BEGAN + 90) == 90)
lib.CODEX_TAIL_MAX = 2048
check("no marker inside the scan cap: unknown, not a guess", lib.codex_busy_seconds("b-far", now=BEGAN + 90) is None)
lib.CODEX_TAIL_CHUNK, lib.CODEX_TAIL_MAX = old_chunk, old_max

check("elapsed time reads plainly", (lib.ago(5), lib.ago(240), lib.ago(3900)) == ("5 s", "4 min", "1 h 5 min"))
cs_busy = S("codex", 20, "/p/x", "x", "b-busy")
cs_idle = S("codex", 21, "/p/x", "x", "b-done")
note = lib.CodexDoor().busy_note(cs_busy, now=BEGAN + 240)
check("door note names the screen, the time and when it reads", cs_busy.label in note and "mid-task" in note and "4 min" in note and "only when its task ends" in note)
check("door note is empty for an idle screen", lib.CodexDoor().busy_note(cs_idle, now=BEGAN + 240) == "")


class BusyDoor(FakeDoor):
    def __init__(self, targets, notes):
        super().__init__(targets)
        self.notes = notes

    def busy_note(self, t):
        note = self.notes[t.pid]
        if isinstance(note, Exception):
            raise note
        return note


m, nc, d = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC(), BusyDoor([sessions[1]], {2: "that Codex is mid-task (started 4 min ago) and reads this only when its task ends"})
run(relay.handle(nc, m, {"claude": d}, lib.PairLimiter()))
check("busy receiver: still delivered and acked, and the sender is told it is mid-task",
      m.acked and nc.sent[0][1]["status"] == "delivered" and nc.sent[0][1]["detail"].startswith("delivered to ") and "Heads up: that Codex is mid-task" in nc.sent[0][1]["detail"])
check("the receiver's own text carries no heads-up", "Heads up" not in d.delivered[0][1])
m, nc, d = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC(), BusyDoor([sessions[1]], {2: ""})
run(relay.handle(nc, m, {"claude": d}, lib.PairLimiter()))
check("idle receiver: reply unchanged", nc.sent[0][1]["detail"] == "delivered to " + sessions[1].label)
m, nc, d = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC(), FakeDoor([sessions[1]])
run(relay.handle(nc, m, {"claude": d}, lib.PairLimiter()))
check("a door with no busy check (Claude): reply unchanged", nc.sent[0][1]["detail"] == "delivered to " + sessions[1].label)
m, nc, d = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC(), BusyDoor([sessions[1]], {2: RuntimeError("log unreadable")})
run(relay.handle(nc, m, {"claude": d}, lib.PairLimiter()))
check("a failing busy check never blocks delivery", m.acked and nc.sent[0][1]["status"] == "delivered" and "Heads up" not in nc.sent[0][1]["detail"])
m, nc, d = FakeMsg("post.downloads.claude", "hello", HDR), FakeNC(), BusyDoor([sessions[0], sessions[1]], {1: "a is mid-task", 2: "b is mid-task"})
run(relay.handle(nc, m, {"claude": d}, lib.PairLimiter()))
check("two busy screens: both named", "Heads up: a is mid-task; b is mid-task" in nc.sent[0][1]["detail"])

# ---- a dead mailbox must fail fast and say so. nats-py retries a refused FIRST connection forever with
# max_reconnect_attempts=0 (and with allow_reconnect=False), so `post` used to freeze its caller when the mailbox was down.
import contextlib  # noqa: E402
import io  # noqa: E402
import time  # noqa: E402
import post as post_cli  # noqa: E402

_probe = socket.socket()
_probe.bind(("127.0.0.1", 0))
_dead_port = _probe.getsockname()[1]
_probe.close()
_saved_url = lib.NATS_URL
lib.NATS_URL = f"nats://127.0.0.1:{_dead_port}"
_err, _code, _t0 = io.StringIO(), None, time.time()
with contextlib.redirect_stderr(_err):
    try:
        asyncio.run(asyncio.wait_for(post_cli.send("claude", "proj", None, "hello", ("claude", "tester", 1)), 30))
    except SystemExit as exc:
        _code = exc.code
    except asyncio.TimeoutError:
        _code = "hung"
lib.NATS_URL = _saved_url
check("post with the mailbox down fails at once with a clear message (it used to freeze)",
      _code == 2 and time.time() - _t0 < 10 and "the post box is not running" in _err.getvalue(),
      f"code={_code} after {time.time() - _t0:.1f}s: {_err.getvalue()[:120]}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
