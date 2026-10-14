#!/usr/bin/env python3
"""Offline tests for Grok support: who a Grok session is (found, or stated with --as / POSTBOX_AS), the inbox door the relay
delivers into, and `post inbox`. No real mailbox, no real Grok, no message leaves this process.
Run: .venv/bin/python tests/test_grok.py"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool (tests/test_telemetry.py covers telemetry itself)
import stat
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib  # noqa: E402
import post as post_cli  # noqa: E402
import relay  # noqa: E402

# This test may run inside a Claude, Codex or Grok session: nothing from that session may leak into what it checks.
for _k in list(os.environ):
    if _k.startswith(("CLAUDE", "CODEX", "GROK", "POSTBOX_")):
        del os.environ[_k]
TMP = tempfile.mkdtemp()
os.environ["POSTBOX_MACHINE"] = "mac-a-test"
os.environ["POSTBOX_SUPPORT_DIR"] = os.path.join(TMP, "support")
PASS = FAIL = 0
run = asyncio.run


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def raises(label, fn, contains=None, kind=Exception):
    try:
        fn()
    except kind as exc:
        check(label, contains is None or contains in str(exc), f"(wrong message: {exc})")
    else:
        check(label, False, "(no error raised)")


_n = [0]


def fresh(name):
    """A new empty folder, for one inbox or one fake Grok home."""
    _n[0] += 1
    path = os.path.join(TMP, f"{name}{_n[0]}")
    os.makedirs(path)
    return path


def use_inbox():
    os.environ["POSTBOX_INBOX_DIR"] = fresh("inbox")


def cli(argv):
    """post.main(argv) -> (exit code or None, stdout, stderr)"""
    out, err, code = io.StringIO(), io.StringIO(), None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            post_cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ who you are, stated outright
print("stated identity (--as, POSTBOX_AS)")
check("agent@project", lib.explicit_identity("grok@myproj") == ("grok", "myproj", None))
check("capitals and spaces become the address form", lib.explicit_identity("Grok@My Project") == ("grok", "my-project", None))
check("agent alone takes the given folder's project", lib.explicit_identity("grok", default_project="Work Folder") == ("grok", "work-folder", None))
with mock.patch.object(lib.os, "getcwd", return_value="/work/.grok"):
    check("agent alone, no folder given: the working folder", lib.explicit_identity("grok") == ("grok", "grok", None))
with mock.patch.object(lib.os, "getcwd", return_value="/"):
    raises("agent alone at the filesystem root cannot name a project", lambda: lib.explicit_identity("grok"), "project", ValueError)
hostile = lib.explicit_identity('gro"k\n@pro$(reboot);j`id`|x\r\nPUB evil')
check("a hostile value is cleaned to letters, digits and hyphens: nothing can reach a header or a shell",
      all(c in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in hostile[0] + hostile[1]), str(hostile))
for bad in ["", "   ", "@proj", "grok@", "a@b@c@d", "!!!", "grok@!!!", "g" * (lib.NAME_MAX + 1) + "@p", "g@" + "p" * (lib.NAME_MAX + 1), "x" * 5000, None]:
    raises(f"not an address: {str(bad)[:20]!r}", lambda b=bad: lib.explicit_identity(b), None, ValueError)
raises("a Mac name is refused (who you are never says which Mac: this Mac adds its own)", lambda: lib.explicit_identity("grok@p@mac-b"), "names a Mac", ValueError)

print("which identity wins")
with mock.patch.object(lib, "my_identity", return_value=("claude", "real", 7)):
    check("--as beats a detected session", lib.resolve_sender("grok@forced")[0] == ("grok", "forced", None))
    os.environ["POSTBOX_AS"] = "grok@envname"
    check("a detected session beats POSTBOX_AS (a stray variable cannot rename a Claude session)", lib.resolve_sender()[0] == ("claude", "real", 7))
with mock.patch.object(lib, "my_identity", return_value=None):
    check("POSTBOX_AS is used when nothing is detected", lib.resolve_sender() == (("grok", "envname", None), "POSTBOX_AS"))
    os.environ["POSTBOX_AS"] = "a@b@c"
    raises("a bad POSTBOX_AS is an error, never a guess", lib.resolve_sender, None, ValueError)
    del os.environ["POSTBOX_AS"]
    check("nothing detected, nothing stated: unknown", lib.resolve_sender() == (None, ""))
    raises("an empty --as is an error, not a fall back", lambda: lib.resolve_sender(""), None, ValueError)

print("post command line")
sent = []


async def fake_send(to_agent, to_project, to_machine, text, me):
    sent.append((to_agent, to_project, to_machine, text, me))
    sys.exit(0)


with mock.patch.object(post_cli, "send", fake_send), mock.patch.object(lib, "my_identity", return_value=None):
    code, _, _ = cli(["post", "--as", "Grok@My Proj", "claude@claude", "hello", "there"])
    check("post --as sends as the cleaned identity, with no process number", code == 0 and sent[-1] == ("claude", "claude", None, "hello there", ("grok", "my-proj", None)), str(sent))
    cli(["post", "--as=grok@p", "claude", "hi"])
    check("--as=NAME works too, and the project defaults to the sender's", sent[-1][:3] == ("claude", "p", None))
    n = len(sent)
    code, _, err = cli(["post", "claude", "a message that says --as grok@x inside"])
    check("--as inside the message text is just text", code == 2 and len(sent) == n and "cannot tell which agent session" in err and "--as" in err, err)
    code, _, err = cli(["post", "--as", "grok@p@mac-b", "claude", "hi"])
    check("--as with a Mac name sends nothing", code == 2 and len(sent) == n and "names a Mac" in err, err)
    code, _, err = cli(["post", "--as", "!!!", "claude", "hi"])
    check("--as that is not an address sends nothing", code == 2 and len(sent) == n and "not who-you-are" in err, err)
    code, _, err = cli(["post", "--as"])
    check("--as with no value is refused", code == 2 and "--as needs" in err, err)
    os.environ["POSTBOX_AS"] = "grok@envproj"
    cli(["post", "claude", "from the variable"])
    check("POSTBOX_AS alone is enough to send", sent[-1][4] == ("grok", "envproj", None) and sent[-1][3] == "from the variable")
    code, out, _ = cli(["post", "whoami"])
    check("whoami says who and how", code is None and out.strip() == "grok@envproj (POSTBOX_AS)", out)
    del os.environ["POSTBOX_AS"]
    code, out, _ = cli(["post", "--as", "grok@p", "whoami"])
    check("whoami with --as", out.strip() == "grok@p (the --as option)", out)


class CaptureNC:
    def __init__(self):
        self.headers = None

    def new_inbox(self):
        return "_INBOX.cap"

    async def subscribe(self, subject):
        class Sub:
            async def next_msg(self, timeout=None):
                return types.SimpleNamespace(data=json.dumps({"status": "delivered", "detail": "x"}).encode())
        return Sub()

    def jetstream(self):
        outer = self

        class JS:
            async def publish(self, subject, payload, headers=None, timeout=None):
                outer.headers = headers
        return JS()

    async def close(self):
        pass


cap = CaptureNC()


async def fake_connect(target, timeout=3):
    return cap


def sent_headers(to_machine, me):
    with mock.patch.object(lib, "connect_fast", fake_connect), mock.patch.object(lib, "peer_target", lambda name: lib.Target("nats://x", None, name)), \
            contextlib.redirect_stdout(io.StringIO()):
        try:
            run(post_cli.send("claude", "proj", to_machine, "hi", me))
        except SystemExit:
            pass
    return cap.headers


h = sent_headers(None, ("grok", "My Proj", None))
check("a stated identity travels as a clean From with no From-Pid", h["From"] == "grok@my-proj" and "From-Pid" not in h, str(h))
h = sent_headers(None, ("grok", "grok", 4321))
check("a detected Grok session sends its process number (so it is never handed its own mail)", h["From"] == "grok@grok" and h["From-Pid"] == "4321", str(h))
h = sent_headers("mac-b", ("grok", "grok", 4321))
check("to another Mac: From carries THIS Mac's name, never a process number", h["From"] == "grok@grok@mac-a-test" and "From-Pid" not in h, str(h))

# ------------------------------------------------------------------ recognising a Grok session
print("recognising Grok")
GH = fresh("grokhome")
os.environ["GROK_HOME"] = GH
SID = "01a00000-0000-7000-8000-000000000a02"


def write_rows(rows):
    Path(GH, "active_sessions.json").write_text(json.dumps(rows) if not isinstance(rows, str) else rows)


write_rows([{"session_id": SID, "pid": 700, "cwd": "/work/My Proj", "opened_at": "2026-09-30T23:15:19Z"}])
PARENTS = {900: "800 /bin/python3", 800: "700 /bin/zsh", 700: "600 grok", 600: "1 -zsh"}
ps_calls = []


def fake_ps(cmd, timeout=5):
    ps_calls.append(cmd)
    pid = int(cmd[2])
    if cmd[3:] == ["-o", "ppid=,comm="]:
        return PARENTS.get(pid, "")
    if cmd[3:] == ["-o", "comm="]:
        return (PARENTS.get(pid, "").split(None, 1) + [""])[1] + "\n"
    return ""


with mock.patch.object(lib, "_run", side_effect=fake_ps), mock.patch.object(lib.os, "getpid", return_value=900), mock.patch.object(lib, "codex_ancestor", return_value=None):
    check("a command under a grok process that Grok lists is that Grok session, project from Grok's own record",
          lib.my_identity() == ("grok", "my-proj", 700))
    with mock.patch.object(lib.os, "getcwd", return_value="/somewhere/else"):
        check("the shell's folder does not change the project", lib.my_identity() == ("grok", "my-proj", 700))
    PARENTS[700] = "600 /home/x/.grok/downloads/grok-1.0.44-macos-aarch64"
    check("the versioned program name is a grok too", lib.my_identity() == ("grok", "my-proj", 700))
    PARENTS[700] = "600 notgrok"
    check("a similar program name is not (a stale row whose number another program reused)", lib.my_identity() is None)
    PARENTS[700] = "600 grok"
    write_rows([{"session_id": SID, "pid": 701, "cwd": "/work/other", "opened_at": "x"}])
    check("a grok ancestor that Grok does not list is refused (never another session's row)", lib.my_identity() is None)
    for label, rows in [("not a list", '{"pid": 700}'), ("unreadable", "not json"), ("pid is a bool", [{"session_id": SID, "pid": True, "cwd": "/w/x"}]),
                        ("relative folder", [{"session_id": SID, "pid": 700, "cwd": "w/x"}]), ("root folder", [{"session_id": SID, "pid": 700, "cwd": "/"}]),
                        ("no session id", [{"pid": 700, "cwd": "/w/x"}]), ("row is not an object", ["x", 3, None])]:
        write_rows(rows)
        check(f"malformed list is refused: {label}", lib.my_identity() is None)
    os.remove(os.path.join(GH, "active_sessions.json"))
    ps_calls.clear()
    check("no Grok list at all: nothing found and no process listing spent", lib.my_identity() is None and ps_calls == [])
    write_rows([{"session_id": SID, "pid": 700, "cwd": "/work/My Proj", "opened_at": "x"}])
    PARENTS.clear()
    PARENTS.update({900: "800 /bin/python3", 800: "900 /bin/zsh"})
    ps_calls.clear()
    check("a process loop is bounded and refused", lib.my_identity() is None and len(ps_calls) <= 3)
    PARENTS.clear()
    PARENTS.update({900: "800 /bin/python3", 800: "700 /bin/zsh", 700: "600 grok", 600: "1 -zsh"})
    with mock.patch.dict(os.environ, {"GROK_AGENT": "1", "CLAUDECODE": "1", "CLAUDE_PID": "321"}), mock.patch.object(lib, "cwd_of", return_value="/workspace/claude-proj"):
        check("Grok started inside a Claude terminal: the nearer Grok ancestor wins over the inherited Claude variables", lib.my_identity() == ("grok", "my-proj", 700))
    with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_PID": "321"}), mock.patch.object(lib, "cwd_of", return_value="/workspace/claude-proj"):
        check("a Claude session is still a Claude session", lib.my_identity() == ("claude", "claude-proj", 321))

print("live Grok sessions")
write_rows([{"session_id": "a", "pid": 700, "cwd": "/work/alpha", "opened_at": "2026-09-30T10:00:00Z"},
            {"session_id": "b", "pid": 701, "cwd": "/work/alpha", "opened_at": "2026-09-30T12:00:00Z"},
            {"session_id": "c", "pid": 702, "cwd": "/work/gone", "opened_at": "2026-09-30T11:00:00Z"},
            {"session_id": "d", "pid": 703, "cwd": "/work/reused", "opened_at": "2026-09-30T11:30:00Z"}])
PARENTS = {700: "1 grok", 701: "1 grok", 703: "1 zsh"}
with mock.patch.object(lib, "_run", side_effect=fake_ps), mock.patch.object(lib, "pid_alive", lambda pid: pid != 702):
    live = lib.grok_sessions()
    check("live sessions only: a dead process and a reused process number are dropped, newest first",
          [s.pid for s in live] == [701, 700] and all(s.agent == "grok" and s.label.startswith("grok@alpha") for s in live), str([s.pid for s in live]))
door = lib.GrokDoor()
two = [lib.Session("grok", 701, "/work/Alpha", "Alpha", "b"), lib.Session("grok", 700, "/work/Alpha", "Alpha", "a"), lib.Session("grok", 5, "/work/beta", "beta", "e")]
check("door: the project's newest session is the one target", [s.pid for s in door.resolve("alpha", sessions=two)] == [701])
check("door: the sender's own session is never the target", [s.pid for s in door.resolve("alpha", exclude_pid=701, sessions=two)] == [700])
check("door: no Grok in that project, no target", door.resolve("gamma", sessions=two) == [])
check("the relay has a door for grok", isinstance(lib.DOORS.get("grok"), lib.GrokDoor) and lib.DOORS["claude"].agent == "claude" and lib.DOORS["codex"].agent == "codex")

# ------------------------------------------------------------------ the inbox
print("inbox")
use_inbox()
NOW = 1_790_000_000.0
f1 = lib.inbox_put("grok", "proj", "first", now=NOW)
f2 = lib.inbox_put("grok", "proj", "second", now=NOW + 1)
check("a message is a file in the agent's own folder, readable only by this user",
      f1.parent == lib.inbox_dir("grok", "proj") and stat.S_IMODE(f1.stat().st_mode) == 0o600 and stat.S_IMODE(f1.parent.stat().st_mode) == 0o700)
got = []
count = lib.inbox_take("grok", "proj", lambda i, t: got.append((i, t)), now=NOW + 2)
check("take hands over oldest first and clears", count == 2 and got == [(1, "first"), (2, "second")] and lib.inbox_list("grok", "proj", NOW + 2) == [])
check("an empty inbox gives nothing", lib.inbox_take("grok", "proj", lambda i, t: got.append(t), now=NOW + 3) == 0)
lib.inbox_put("grok", "proj", "keep me", now=NOW)


def failing_print(i, t):
    raise RuntimeError("terminal closed")


raises("a failing print is passed up", lambda: lib.inbox_take("grok", "proj", failing_print, now=NOW + 1), "terminal closed")
check("and the message is still there (deleted only after it was shown)", len(lib.inbox_list("grok", "proj", NOW + 1)) == 1)
check("inboxes are separate per agent and project", lib.inbox_list("grok", "other", NOW) == [] and lib.inbox_list("claude", "proj", NOW) == [])
for parts in [("grok", "../../etc"), ("../x", "p"), ("grok", "a/b\\c")]:
    check(f"a hostile name cannot leave the inbox root: {parts}", lib.inbox_dir(*parts).parent == lib.inbox_root())

print("inbox limits")
use_inbox()
for i in range(lib.INBOX_MAX_MESSAGES):
    lib.inbox_put("grok", "full", f"m{i}", now=NOW + i * 0.001)
raises("the count cap: one more than the cap is refused", lambda: lib.inbox_put("grok", "full", "one too many", now=NOW + 1), "inbox full", lib.InboxFull)
check("and nothing was written for it", len(lib.inbox_list("grok", "full", NOW + 1)) == lib.INBOX_MAX_MESSAGES)
lib.inbox_take("grok", "full", lambda i, t: None, now=NOW + 1)
check("reading makes room again", lib.inbox_put("grok", "full", "fits now", now=NOW + 2).exists())
raises("the size cap: a message over the limit is refused", lambda: lib.inbox_put("grok", "big", "x" * (lib.INBOX_MAX_BYTES + 1)), "too big", ValueError)
check("a message at the limit is stored", lib.inbox_put("grok", "big", "x" * lib.INBOX_MAX_BYTES).exists())

use_inbox()
lib.inbox_put("grok", "age", "old", now=NOW)
check("expiry: still there just inside an hour", len(lib.inbox_list("grok", "age", NOW + lib.MAX_AGE_S - 1)) == 1)
check("expiry: gone after an hour, and the file is deleted", lib.inbox_list("grok", "age", NOW + lib.MAX_AGE_S + 1) == [] and not any(lib.inbox_dir("grok", "age").iterdir()))
lib.inbox_put("grok", "age", "old2", now=NOW)
lib.inbox_put("grok", "age2", "old3", now=NOW)
fresh_file = lib.inbox_put("grok", "age2", "new", now=NOW + 3000)
check("the sweep removes expired mail from every inbox and keeps the rest", lib.inbox_sweep(now=NOW + lib.MAX_AGE_S + 1) == 2 and fresh_file.exists())

use_inbox()
folder = lib.inbox_dir("grok", "stray")
folder.mkdir(parents=True)
outside = Path(TMP) / "outside-secret.txt"
outside.write_text("do not read me")
link = folder / f"{int(NOW * 1000):013d}-aaaaaaaaaaaa.msg"
link.symlink_to(outside)
(folder / "notes.txt").write_text("not a message")
(folder / f"{int(NOW * 1000):013d}-bbbbbbbbbbbb.msg").write_bytes(b"y" * (lib.INBOX_MAX_BYTES + 5))
seen = []
lib.inbox_take("grok", "stray", lambda i, t: seen.append(t), now=NOW + 1)
check("a symlink, a stray file and an oversize file are never shown", seen == [] and outside.exists() and (folder / "notes.txt").exists())

# ------------------------------------------------------------------ the relay delivers into the inbox
print("relay and the Grok door")


class FakeMsg:
    def __init__(self, subject, data, headers, delivered=1):
        self.subject, self.data, self.headers = subject, data.encode(), headers
        self.metadata = types.SimpleNamespace(num_delivered=delivered)
        self.acked = self.naked = self.termed = None

    async def ack(self):
        self.acked = True

    async def nak(self, delay=None):
        self.naked = delay

    async def term(self):
        self.termed = True


class FakeNC:
    def __init__(self):
        self.sent = []

    async def publish(self, subj, data):
        self.sent.append((subj, json.loads(data)))

    async def flush(self, timeout=None):
        pass


HDR = {"From": "claude@claude", "From-Pid": "11", "Reply-Inbox": "_INBOX.x"}
GS = lib.Session("grok", 75656, "/home/x/.grok", ".grok", SID)


def deliver(text="hello grok", hdr=HDR, sessions=(GS,), delivered=1, limiter=None, subject="post.grok.grok"):
    m, nc = FakeMsg(subject, text, dict(hdr), delivered), FakeNC()
    with mock.patch.object(lib, "grok_sessions", lambda: list(sessions)):
        run(relay.handle(nc, m, lib.DOORS, limiter or lib.PairLimiter()))
    return m, nc


use_inbox()
m, nc = deliver()
body = [p.read_text() for p in lib.inbox_list("grok", ".grok")]
check("a live Grok session: acked and the sender is told delivered", m.acked and nc.sent[0][1]["status"] == "delivered", str(nc.sent))
check("the answer says what delivered means for Grok: read from its inbox, not woken",
      nc.sent[0][1]["detail"].startswith("delivered to grok@.grok (pid 75656)") and "cannot be woken" in nc.sent[0][1]["detail"] and "post inbox" in nc.sent[0][1]["detail"], nc.sent[0][1]["detail"])
check("the message is waiting in the inbox, with the way to answer and the inbox wording",
      len(body) == 1 and "Message from claude@claude (post box):\nhello grok" in body[0] and 'post claude@claude "your reply"' in body[0] and "run `post inbox`" in body[0], str(body))
check("the Claude and Codex wording is unchanged", "replies arrive as new messages" in lib.render("a@b", "x") and "post inbox" not in lib.render("a@b", "x"))

use_inbox()
m, nc = deliver(sessions=())
check("no Grok session in that project: queued (kept by the mailbox, retried), nothing in any inbox",
      m.naked == 10 and not m.acked and nc.sent[0][1]["status"] == "queued" and lib.inbox_list("grok", ".grok") == [] and lib.inbox_list("grok", "grok") == [], str(nc.sent))
m, nc = deliver(sessions=(), delivered=4)
check("retry rounds stay quiet", m.naked == 10 and nc.sent == [])
m, nc = deliver(hdr={**HDR, "From-Pid": "75656"}, subject="post.grok.grok")
check("a Grok session is never handed its own message", m.naked == 10 and nc.sent[0][1]["status"] == "queued" and lib.inbox_list("grok", ".grok") == [])
m, nc = deliver(subject="post.other.grok")
check("another project's Grok is not reached", nc.sent[0][1]["status"] == "queued" and lib.inbox_list("grok", ".grok") == [])

use_inbox()
for i in range(lib.INBOX_MAX_MESSAGES):
    lib.inbox_put("grok", ".grok", f"unread {i}")
m, nc = deliver("one more")
check("a full inbox: the sender is told queued, the message stays in the mailbox for retry and nothing is lost or dropped",
      m.naked == 10 and not m.acked and not m.termed and nc.sent[0][1]["status"] == "queued" and "inbox full" in nc.sent[0][1]["detail"], str(nc.sent))

use_inbox()
m, nc = deliver(limiter=lib.PairLimiter(limit=0))
check("the stop rule still applies to Grok mail", m.termed and nc.sent[0][1]["status"] == "refused" and "stop rule" in nc.sent[0][1]["detail"] and lib.inbox_list("grok", ".grok") == [])
m, nc = deliver("x" * (lib.MAX_TEXT_BYTES + 1))
check("the size cap still applies to Grok mail", m.termed and nc.sent[0][1]["status"] == "refused" and "too long" in nc.sent[0][1]["detail"] and lib.inbox_list("grok", ".grok") == [])
m, nc = deliver(hdr={"From": "Claude@My Proj@Mac-B", "Reply-Inbox": "_INBOX.x"})
check("mail from another Mac: the sender address is cleaned and shown in full for the reply",
      nc.sent[0][1]["status"] == "delivered" and "Message from claude@my-proj@mac-b (post box)" in lib.inbox_list("grok", ".grok")[-1].read_text())
m, nc = deliver(hdr={"From": "grok@grok$(reboot)\nPUB x", "Reply-Inbox": "_INBOX.x"})
check("a hostile From cannot put a newline or shell character into what Grok reads",
      "\nPUB x" not in lib.inbox_list("grok", ".grok")[-1].read_text() and "$(" not in lib.inbox_list("grok", ".grok")[-1].read_text())
m, nc = deliver(subject="post.grok.gemini")
check("an agent with no door is still refused in words", m.termed and nc.sent[0][1]["status"] == "refused" and "no delivery door" in nc.sent[0][1]["detail"])

print("who lists Grok")
box = []


class WhoNC(FakeNC):
    async def publish(self, subj, data):
        box.append(json.loads(data))


with mock.patch.object(lib, "claude_sessions", lambda: []), mock.patch.object(lib, "codex_sessions", lambda: []), mock.patch.object(lib, "grok_sessions", lambda: [GS]):
    run(relay.answer_who(WhoNC(), types.SimpleNamespace(reply="_INBOX.who")))
    check("another Mac asking who is here sees the Grok session, as an address it can type", box and box[0]["sessions"] == [{"agent": "grok", "project": "grok", "pid": 75656}], str(box))
    with mock.patch.object(lib, "my_identity", return_value=("grok", ".grok", 75656)):
        code, out, _ = cli(["post", "who"])
    check("post who lists it and marks it as this session", "grok@grok" in out and "pid 75656" in out and "<- this session" in out, out)

# ------------------------------------------------------------------ post inbox, the command
print("post inbox")
use_inbox()
lib.inbox_put("grok", "proj", "Message from claude@claude (post box):\nfirst body\n", now=NOW)
lib.inbox_put("grok", "proj", "Message from codex@x (post box):\nsecond body\n", now=NOW + 1)
with mock.patch.object(lib.time, "time", return_value=NOW + 2):
    code, out, err = cli(["post", "--as", "grok@proj", "inbox"])
check("post inbox prints every waiting message, oldest first", code is None and out.index("first body") < out.index("second body") and "--- message 1 ---" in out and "--- message 2 ---" in out, out)
with mock.patch.object(lib.time, "time", return_value=NOW + 3):
    code, out, err = cli(["post", "--as", "grok@proj", "inbox"])
check("and clears them: the next call says nothing is waiting", "No messages waiting for grok@proj" in out and "body" not in out, out)
lib.inbox_put("grok", "proj", "for proj only", now=NOW + 4)
with mock.patch.object(lib.time, "time", return_value=NOW + 5):
    code, out, _ = cli(["post", "--as", "grok@elsewhere", "inbox"])
check("another identity sees none of it", "No messages waiting for grok@elsewhere" in out and "for proj only" not in out, out)
code, _, err = cli(["post", "--as", "grok@proj", "inbox", "extra", "words"])
check("post inbox with stray words is refused", code == 2 and "takes no words" in err, err)
code, _, err = cli(["post", "inbox"])
check("post inbox without an identity says how to state one", code == 2 and "--as" in err and "POSTBOX_AS" in err, err)

print("post inbox --watch (the doorbell)")
use_inbox()
lines = []
lib.inbox_put("grok", "proj", "Message from claude@claude (post box):\nSECRET-BODY-TEXT\n")


def ring(text, flush=False):
    lines.append(text)
    if len(lines) == 1:
        lib.inbox_put("grok", "proj", "a second one")


post_cli.watch_inbox("grok", "proj", interval=0, rounds=4, out=ring)
check("one line when mail is first seen, one more when new mail arrives, none for what was already announced", len(lines) == 2, str(lines))
check("the line says where to read it and carries no message text", "post inbox" in lines[0] and "SECRET-BODY-TEXT" not in " ".join(lines) and "1 unread" in lines[0] and "2 unread" in lines[1], str(lines))
check("watching never clears anything", len(lib.inbox_list("grok", "proj")) == 2)
quiet = []
post_cli.watch_inbox("grok", "empty", interval=0, rounds=3, out=lambda t, flush=False: quiet.append(t))
check("an empty inbox rings nothing", quiet == [])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
