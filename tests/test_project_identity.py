#!/usr/bin/env python3
"""Offline tests: every project has its own mailbox, and a session can say which project it belongs to.
Run: .venv/bin/python tests/test_project_identity.py

A session's project is, in this order: (a) a live declaration made with `post join` by that exact process, (b) the nearest
`.postbox-project` file in its folder or an ancestor (the walk stops at $HOME), (c) the folder's name.

Everything here is fake: a temporary HOME, a temporary registration folder (POSTBOX_REG_DIR), made-up process numbers and start times,
a throwaway Codex thread table. No real session, ~/.codex, or ~/Library/Application Support/postbox is touched, and `post join` is only
ever run against a pretend session.
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
import sqlite3
import stat
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

os.environ["POSTBOX_TELEMETRY"] = "0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)

TMP = os.path.realpath(tempfile.mkdtemp(prefix="postbox-project-identity-"))
HOME = os.path.join(TMP, "home")
REG = os.path.join(TMP, "reg")
SUPPORT = os.path.join(TMP, "support")
GROK = os.path.join(TMP, "grok")
SOCKS = os.path.join(TMP, "socks")
for d in (HOME, SUPPORT, GROK, SOCKS):
    os.makedirs(d)
BASE_ENV = {"HOME": HOME, "PATH": os.environ.get("PATH", ""), "POSTBOX_TELEMETRY": "0", "POSTBOX_REG_DIR": REG, "POSTBOX_SUPPORT_DIR": SUPPORT,
            "POSTBOX_MACHINE": "mac-a", "GROK_HOME": GROK}
os.environ.update(BASE_ENV)

import lib  # noqa: E402
import post  # noqa: E402
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


# ---- fake processes: which numbers are alive, when each started, and which folder each is in
ALIVE, STARTS, CWDS = set(), {}, {}
lib.pid_alive = lambda pid: pid in ALIVE
lib.process_start = lambda pid: STARTS.get(pid)
lib.cwd_of = lambda pid: CWDS.get(pid)
lib.process_table = lambda: {}
lib.process_age_s = lambda pid: 600.0


def folder(*parts):
    path = os.path.join(HOME, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def proc(pid, cwd, start="S1"):
    ALIVE.add(pid)
    STARTS[pid] = start
    CWDS[pid] = cwd
    return pid


def marker(where, text):
    Path(where, lib.PROJECT_MARKER).write_text(text)


@contextlib.contextmanager
def env(**extra):
    """The environment of a pretend session: only what post looks at, nothing from the real one."""
    with mock.patch.dict(os.environ, {**BASE_ENV, **extra}, clear=True):
        yield


def cli(*argv, **extra):
    """Run `post <argv>` for a pretend session. Returns (exit code, stdout, stderr, telemetry events as (args, kwargs))."""
    out, err, events, code = io.StringIO(), io.StringIO(), [], 0
    with env(**extra), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(post.tel, "emit", lambda *a, **k: events.append((a, k)) or True):
        try:
            post.run(["post", *argv])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return code, out.getvalue(), err.getvalue(), events


def reg_files():
    return sorted(os.listdir(REG)) if os.path.isdir(REG) else []


print("-- (a) precedence: declaration > marker > folder")
work = folder("work")
mac = folder("work", "mac")
P = proc(5001, mac)
info = lib.project_info("codex", P, mac)
check("no marker, no declaration: the folder name", (info.project, info.source) == ("mac", "folder"), info)
marker(work, "ahead\n")
info = lib.project_info("codex", P, mac)
check("a marker in an ancestor folder beats the folder name", (info.project, info.source, info.where) == ("ahead", "marker", os.path.join(work, ".postbox-project")), info)
lib.declare("codex", P, "decl")
info = lib.project_info("codex", P, mac)
check("a live declaration beats the marker and the folder", (info.project, info.source) == ("decl", "declaration"), info)
check("project_of is the same answer as project_info", lib.project_of("codex", P, mac) == "decl")
check("a declaration for another agent with the same process number is not this one's", lib.project_of("claude", P, mac) == "ahead")
check("no process number (a stated --as identity) never has a declaration", lib.project_of("codex", None, mac) == "ahead")
lib.undeclare(P)
check("without the declaration it is the marker again", lib.project_of("codex", P, mac) == "ahead")
os.unlink(os.path.join(work, ".postbox-project"))
check("without the marker it is the folder again", lib.project_of("codex", P, mac) == "mac")
check("the answer always goes through norm()", lib.project_of("claude", None, folder("work", "My Project_2")) == "my-project-2")

print("-- (b) marker: nearest ancestor, stops at $HOME, unusable ones ignored")
deep = folder("m1", "a", "b", "c")
marker(folder("m1", "a"), "# which project this is\nfrom-ancestor   # trailing comment\n")
info = lib.project_info("claude", None, deep)
check("found three folders up; comment lines and trailing comments are ignored", (info.project, info.source) == ("from-ancestor", "marker"), info)
marker(folder("m1", "a", "b"), "nearer\n")
check("the NEAREST marker wins", lib.project_of("claude", None, deep) == "nearer")
marker(HOME, "home-wide\n")
check("a marker in $HOME itself is not used (it would rename every session below it)", lib.project_of("claude", None, folder("m2", "deep")) == "deep")
Path(TMP, lib.PROJECT_MARKER).write_text("above-home\n")
check("a marker above $HOME is not used either", lib.project_of("claude", None, folder("m3")) == "m3")
os.unlink(os.path.join(HOME, ".postbox-project"))
os.unlink(os.path.join(TMP, ".postbox-project"))
for label, text in (("an address with @", "a@b\n"), ("only comments", "# nothing here\n"), ("an empty file", ""), ("the word unknown", "unknown\n"),
                    ("a name over 64 letters", "x" * 80 + "\n"), ("a control character", "bad\x01name\n")):
    where = folder("bad", label.replace(" ", "-"))
    marker(where, text)
    info = lib.project_info("claude", None, where)
    check(f"unusable marker ({label}) is ignored with a note, falling back to the folder", info.source == "folder" and info.project == lib.norm(os.path.basename(where))
          and "is not a usable project name" in info.note, info)
near_bad = folder("far", "near")
marker(folder("far"), "far-valid\n")
marker(near_bad, "a@b\n")
info = lib.project_info("claude", None, near_bad)
check("an unusable near marker is skipped, a valid farther one is used, and the note says so", (info.project, info.source) == ("far-valid", "marker") and info.note, info)
check("a folder outside $HOME with no marker does not raise", lib.project_of("claude", None, "/nonexistent-postbox-test/x") == "x")
check("no cwd at all does not raise", lib.project_of("claude", None, None) == "unknown")

print("-- (c) pid reuse: a declaration for a different process start is ignored")
c_cwd = folder("c1", "reuse")
P = proc(5002, c_cwd, "Fri Oct  2 10:00:00 2026")
lib.declare("codex", P, "p1")
check("same process, same start: the declaration counts", lib.project_of("codex", P, c_cwd) == "p1")
STARTS[P] = "Fri Oct  2 11:11:11 2026"   # the number was reused by a new process
check("a different start time for that number: ignored, the folder wins", lib.project_of("codex", P, c_cwd) == "reuse")
check("...and no note about it (it is simply not this process's)", lib.project_info("codex", P, c_cwd).note == "")

print("-- (d) a dead process's declaration is ignored, then swept")
d_cwd = folder("d1", "gone")
P_dead = proc(5003, d_cwd)
P_live = proc(5004, d_cwd)
lib.declare("codex", P_dead, "ghost")
lib.declare("codex", P_live, "alive")
check("while it runs, it counts", lib.project_of("codex", P_dead, d_cwd) == "ghost")
ALIVE.discard(P_dead)
check("process gone: ignored", lib.project_of("codex", P_dead, d_cwd) == "gone")
Path(REG, "777.json").write_text("{not json")
Path(REG, "888.json").write_text(json.dumps({"agent": "codex", "pid": 999, "start": "S1", "project": "x"}))   # file name and pid disagree
before = reg_files()
swept = lib.reg_sweep()
after = reg_files()
check("sweep removes the dead one, the garbage file, the mismatching one, and the stale 5002 reuse",
      "5003.json" not in after and "777.json" not in after and "888.json" not in after and "5002.json" not in after, (before, after))
check("sweep keeps the live one and says how many it removed", "5004.json" in after and lib.project_of("codex", P_live, d_cwd) == "alive" and swept == 4, (swept, after))
with env(POSTBOX_REG_DIR=os.path.join(TMP, "nope")):
    check("sweeping an absent folder is fine", lib.reg_sweep() == 0)
check("the relay runs the sweep on its heartbeat", "lib.reg_sweep" in Path(ROOT, "postbox", "relay.py").read_text())
mode_dir, mode_file = stat.S_IMODE(os.stat(REG).st_mode), stat.S_IMODE(os.stat(os.path.join(REG, "5004.json")).st_mode)
check("registrations: folder mode 700, file mode 600", (mode_dir, mode_file) == (0o700, 0o600), (oct(mode_dir), oct(mode_file)))
entry = json.loads(Path(REG, "5004.json").read_text())
check("a registration holds agent, pid, start, project, declared_at", sorted(entry) == ["agent", "declared_at", "pid", "project", "start"] and entry["start"] == "S1", entry)

print("-- (e) a codex session in a folder called mac declares project codex")
e_cwd = folder("work", "tools", "mac")
E_PID = 27449
proc(E_PID, e_cwd)
THREAD = "01a00000-0000-7000-8000-000000000a01"
DB = os.path.join(TMP, "state_5.sqlite")
con = sqlite3.connect(DB)
con.execute("create table threads (id text primary key, cwd text, archived integer, updated_at integer)")
con.execute("insert into threads values (?,?,?,?)", (THREAD, e_cwd, 0, int(time.time())))
con.commit()
lib.codex_state_db = lambda: DB
TABLE = {E_PID: (1, ["node", "/x/bin/codex"])}


class Msg:
    def __init__(self, project, agent="codex"):
        self.subject, self.data = f"post.{project}.{agent}", b"hello"
        self.headers = {"From": "claude@myproj", "From-Pid": "1", "Reply-Inbox": "_INBOX.x"}
        self.metadata = types.SimpleNamespace(num_delivered=1, sequence=types.SimpleNamespace(stream=1, consumer=1))
        self.acked = self.naked = None

    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): pass


class NC:
    def __init__(self): self.sent = []
    async def publish(self, subj, data): self.sent.append(json.loads(data))
    async def flush(self, timeout=None): pass


class RecordingDoor(lib.CodexDoor):
    """The real Codex door fed the real session finder (fake process table); deliver() records instead of running `codex`."""
    def __init__(self): self.handed, self.projections = [], []
    def resolve(self, project, exclude_pid=None, sessions=None):
        self.projections.append("resolve")
        return super().resolve(project, exclude_pid, lib.codex_sessions(TABLE))
    def waiting(self, project, exclude_pid=None, sessions=None):
        self.projections.append("waiting")
        return super().waiting(project, exclude_pid, lib.codex_sessions(TABLE))
    def busy_note(self, session, now=None): return ""
    def deliver(self, session, text, timeout=25.0): self.handed.append(session.target)


def mail(door, project):
    nc, m = NC(), Msg(project)
    with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(relay.tel, "emit", lambda *a, **k: True), env():
        asyncio.run(relay.handle(nc, m, {"codex": door}, lib.PairLimiter(), lib.PairLimiter(limit=100)))
    return nc.sent[0], m


door = RecordingDoor()
a, _ = mail(door, "mac")
check("before: codex@mac reaches the session", a["status"] == "delivered" and door.handed == [THREAD], a)
check("candidate discovery honors the overridden synthetic projections", door.projections == ["resolve", "waiting"], door.projections)
a, _ = mail(door, "codex")
check("before: codex@codex finds nobody (queued, nobody_there)", a["status"] == "queued" and a["reason"] == "nobody_there", a)
lib.declare("codex", E_PID, "codex")
door = RecordingDoor()
check("declared: the session is listed as codex, source declaration, real folder kept",
      [(s.project, s.source, os.path.basename(s.cwd)) for s in lib.codex_sessions(TABLE)] == [("codex", "declaration", "mac")])
a, m = mail(door, "codex")
check("declared: mail to codex@codex reaches it", a["status"] == "delivered" and door.handed == [THREAD] and m.acked, a)
door = RecordingDoor()
a, m = mail(door, "mac")
check("declared: mail to codex@mac does NOT reach it; it is queued (nobody_there) and kept", a["status"] == "queued" and a["reason"] == "nobody_there"
      and door.handed == [] and m.naked == 10 and not m.acked, a)
lib.undeclare(E_PID)
door = RecordingDoor()
a, _ = mail(door, "mac")
check("after leave: codex@mac reaches it again", a["status"] == "delivered" and door.handed == [THREAD], a)
a, _ = mail(door, "codex")
check("after leave: codex@codex is queued again", a["status"] == "queued", a)

print("-- (f) two different folders both named x are distinct once one has a marker")
fx1, fx2 = folder("f1", "x"), folder("f2", "x")
s1, s2 = lib._session("claude", proc(5010, fx1), fx1, "a.sock"), lib._session("claude", proc(5011, fx2), fx2, "b.sock")
check("both folders called x share one mailbox without a marker", [s.pid for s in lib.ClaudeDoor().resolve("x", sessions=[s1, s2])] == [5010, 5011])
marker(fx2, "x-two\n")
s2 = lib._session("claude", 5011, fx2, "b.sock")
check("with a marker in one of them they are separate mailboxes",
      [s.pid for s in lib.ClaudeDoor().resolve("x", sessions=[s1, s2])] == [5010] and [s.pid for s in lib.ClaudeDoor().resolve("x-two", sessions=[s1, s2])] == [5011])

print("-- (g) the sender's own address uses the declared project")
g_cwd = folder("g1", "mac")
G = proc(5020, g_cwd)
with env(CLAUDECODE="1", CLAUDE_PID=str(G)):
    before = lib.my_identity()
    lib.declare("claude", G, "declared-x")
    after = lib.my_identity()
    trace = lib.IDENTITY_PROJECT.get("info")
    sent = []

    async def fake_send(to_agent, to_project, to_machine, text, me):
        sent.append((to_agent, to_project, me))
    out = io.StringIO()
    with mock.patch.object(post, "send", fake_send), contextlib.redirect_stdout(out):
        post.main(["post", "codex", "hi"])
check("before the declaration the sender is claude@mac", before == ("claude", "mac", G), before)
check("after it the sender is claude@declared-x, same process", after == ("claude", "declared-x", G) and trace.source == "declaration", (after, trace))
check("a send to plain `codex` goes to MY (declared) project, and the From header says so",
      sent and sent[0][:2] == ("codex", "declared-x") and post.mail_headers(sent[0][2], False)["From"] == "claude@declared-x", sent)
lib.undeclare(G)

print("-- post join / post leave / post join (no name), end to end for a pretend session")
J_cwd = folder("j1", "mac")
J = proc(5030, J_cwd)
sess = dict(CLAUDECODE="1", CLAUDE_PID=str(J))
code, out, err, ev = cli("join", **sess)
check("`post join` alone: says the project and where it came from", code == 0 and "project is mac, from the folder name" in out, (code, out, err))
check("...and writes a post_join event with op show", ev and ev[0][0][0] == "post_join" and ev[0][1]["op"] == "show" and ev[0][1]["source"] == "folder", ev)
code, out, err, ev = cli("join", "Declared X", **sess)
join_ev = [e for e in ev if e[0][0] == "post_join"]
check("join: ok, says the cleaned name, what changes and how to undo it", code == 0 and "now belongs to project declared-x" in out and "no longer reaches it" in out and "post leave" in out, (code, out, err))
check("join: one registration file for this process", reg_files().count("5030.json") == 1 and json.loads(Path(REG, "5030.json").read_text())["project"] == "declared-x")
check("join: post_join event carries agent, project, source, result, previous project, never anything else secret",
      len(join_ev) == 1 and join_ev[0][1]["agent"] == "claude" and join_ev[0][1]["project"] == "declared-x" and join_ev[0][1]["source"] == "declaration"
      and join_ev[0][1]["previous_project"] == "mac" and join_ev[0][1]["result"] == "ok" and join_ev[0][1]["exit_code"] == 0, join_ev)
code, out, err, _ = cli("whoami", **sess)
check("whoami: the declared project and its source", code == 0 and out.startswith("claude@declared-x (") and "declared with post join" in out, out)
code, out, err, _ = cli("join", **sess)
check("join with no name now says declared", "project is declared-x, declared with post join" in out, out)
code, out, err, ev = cli("leave", **sess)
leave_ev = [e for e in ev if e[0][0] == "post_leave"]
check("leave: back to the folder name, file removed", code == 0 and "back to project mac" in out and "5030.json" not in reg_files(), (code, out, err, reg_files()))
check("leave: post_leave event says removed and the project it went back to", len(leave_ev) == 1 and leave_ev[0][1]["removed"] is True and leave_ev[0][1]["project"] == "mac"
      and leave_ev[0][1]["previous_project"] == "declared-x" and leave_ev[0][1]["result"] == "ok", leave_ev)
code, out, err, ev = cli("leave", **sess)
check("leave with nothing declared: says so, exits 0, removed False", code == 0 and "nothing to leave" in out and [e for e in ev if e[0][0] == "post_leave"][0][1]["removed"] is False, (code, out))
marker(J_cwd, "from-marker\n")
code, out, err, _ = cli("leave", **sess)
check("leave when a marker file sets the project: says the marker still applies", "from the marker file" in out and "from-marker" in out, out)
os.unlink(os.path.join(J_cwd, ".postbox-project"))

print("-- (h) refusals in plain words")
for label, name in (("an address with @", "a@b"), ("an empty name", ""), ("blanks only", "   "), ("the word unknown", "unknown"), ("over 64 letters", "x" * 80), ("a newline", "bad\nname")):
    code, out, err, ev = cli("join", name, **sess)
    refused = [e for e in ev if e[0][0] == "post_join"]
    check(f"join {label}: refused, nothing written", code == 2 and "is not a project name" in err and "Nothing was changed" in err and "5030.json" not in reg_files()
          and refused and refused[0][1]["result"] == "refused" and refused[0][1]["reason"] == "bad_project_name", (code, err, reg_files()))
code, out, err, ev = cli("join", "one", "two", **sess)
check("join with two words: refused, nothing written", code == 2 and "one project name" in err and "5030.json" not in reg_files(), (code, err))
code, out, err, ev = cli("join", "someproject")   # no session variables at all
check("unidentified caller: refused in plain words, no file, post_identity and a refused post_join written",
      code == 2 and "cannot tell which agent session this is" in err and "nothing was changed" in err and reg_files().count("5030.json") == 0
      and any(e[0][0] == "post_identity" for e in ev) and [e for e in ev if e[0][0] == "post_join"][0][1]["reason"] == "cannot_tell_sender", (code, err))
code, out, err, ev = cli("leave")
check("unidentified caller can not leave either", code == 2 and "cannot tell which agent session this is" in err, (code, err))
code, out, err, ev = cli("--as", "grok@somewhere", "join", "other", **sess)
check("--as names no process, so it can not join: refused", code == 2 and "--as names no process" in err and "5030.json" not in reg_files()
      and [e for e in ev if e[0][0] == "post_join"][0][1]["reason"] == "needs_session", (code, err))
check("a declaration whose project name was damaged on disk is ignored with a note, not an exception",
      (lambda: (lib.declare("claude", J, "fine"), Path(REG, "5030.json").write_text(json.dumps({"agent": "claude", "pid": J, "start": "S1", "project": "a@b"})),
                lib.project_info("claude", J, J_cwd))[2])().note.startswith("ignored the declaration"))
lib.undeclare(J)

print("-- (i) post who shows declared projects and the source")
w_mac, w_mark, w_plain, w_bad = folder("w1", "mac"), folder("w1", "mark"), folder("w1", "plain"), folder("w1", "bad")
marker(w_mark, "markedproj\n")
marker(w_bad, "a@b\n")
for pid, cwd in ((5040, w_mac), (5041, w_mark), (5042, w_plain), (5043, w_bad)):
    proc(pid, cwd)
    Path(SOCKS, f"{pid}.sock").write_text("")
lib.declare("claude", 5040, "declared-x")
real_run = lib._run
with mock.patch.object(lib, "SOCK_DIR", SOCKS), mock.patch.object(lib, "_run", lambda cmd, timeout=5: "claude\n" if cmd[:1] == ["ps"] and "command=" in cmd else ""):
    code, out, err, _ = cli("who")
lines = {int(l.split("pid ")[1].split()[0]): l for l in out.splitlines() if "pid " in l}
check("who: the declared session is listed under its declared project, tagged (declared), with its real folder", code == 0 and lines[5040].split()[0] == "claude@declared-x"
      and "(declared)" in lines[5040] and "[folder mac]" in lines[5040], out)
check("who: a marker session is tagged (marker) and a plain one has no tag and no folder suffix", "claude@markedproj" in lines[5041] and "(marker)" in lines[5041] and "[folder mark]" in lines[5041]
      and "(declared)" not in lines[5042] and "(marker)" not in lines[5042] and "[folder" not in lines[5042], out)
check("who: an unusable marker leaves the folder name and prints a one-line note", "claude@bad" in lines[5043] and "note: ignored" in out and "not a usable project name" in out, out)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
