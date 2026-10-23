#!/usr/bin/env python3
"""Offline tests for per-session addressing: agent@project#name, the held-message bounce, and `post pick`.
Run: .venv/bin/python tests/test_session_addressing.py

Nothing here opens a real mailbox, a real Claude Code session or a real Codex database: sessions, the mailbox and the doors are fakes, and
every folder the code writes to (held messages, telemetry, Claude's session files) is a temporary one.
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
import sqlite3
import sys
import tempfile
import time
import types

TMP = tempfile.mkdtemp(prefix="postbox-session-test-")
os.environ["POSTBOX_SUPPORT_DIR"] = os.path.join(TMP, "support")
os.environ["POSTBOX_HELD_DIR"] = os.path.join(TMP, "held")
os.environ["POSTBOX_CLAUDE_SESSIONS_DIR"] = os.path.join(TMP, "claude-sessions")
os.environ["POSTBOX_TELEMETRY_SPOOL"] = os.path.join(TMP, "spool")
os.environ["POSTBOX_MACHINE"] = "mac-a"
os.makedirs(os.environ["POSTBOX_CLAUDE_SESSIONS_DIR"])

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib  # noqa: E402
import post  # noqa: E402
import relay  # noqa: E402
import telemetry  # noqa: E402

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


def raises(label, fn, needle=""):
    try:
        fn()
    except ValueError as e:
        check(label, needle in str(e), str(e))
    else:
        check(label, False, "did not raise")


def S(pid, name, project="project-a", sid=None, agent="claude"):
    """A fake live session, as lib.claude_sessions() would list it."""
    return lib.Session(agent, pid, f"/work/{project}", project, f"/s/{pid}", True, "folder", "", "", name, sid if sid is not None else f"{pid:06x}"[-6:])


class Msg:
    def __init__(self, subject, text, headers, delivered=1, seq=1):
        self.subject, self.data, self.headers = subject, text.encode(), headers
        self.metadata = types.SimpleNamespace(num_delivered=delivered, sequence=types.SimpleNamespace(stream=seq, consumer=seq))
        self.acked = self.naked = self.termed = None

    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): self.termed = True


class JS:
    def __init__(self, nc): self.nc = nc
    async def publish(self, subject, data, headers=None, timeout=None): self.nc.published.append((subject, data.decode(), dict(headers or {})))


class NC:
    def __init__(self): self.sent, self.published = [], []
    async def publish(self, subj, data): self.sent.append((subj, json.loads(data)))
    async def flush(self, timeout=None): pass
    def jetstream(self): return JS(self)

    @property
    def answer(self): return self.sent[0][1] if self.sent else None


class Door(lib.ClaudeDoor):
    """The real Claude door (real resolve and real per_session rule) over a fixed list of sessions, recording what it would deliver."""
    def __init__(self, sessions): self.sessions, self.got = sessions, []
    def resolve(self, project, exclude_pid=None, sessions=None):
        return lib.ClaudeDoor.resolve(self, project, exclude_pid, sessions=self.sessions)
    def deliver(self, session, text, timeout=3.0): self.got.append((session.pid, text))


class InboxDoor(lib.GrokDoor):
    def __init__(self, sessions): self.sessions, self.got = sessions, []
    def resolve(self, project, exclude_pid=None, sessions=None): return lib.GrokDoor.resolve(self, project, exclude_pid, sessions=self.sessions)
    def deliver(self, session, text, timeout=None): self.got.append((session.pid, text))


def handle(msg, doors, nc=None):
    nc = nc or NC()
    with contextlib.redirect_stdout(io.StringIO()):
        run(relay.handle(nc, msg, doors, lib.PairLimiter(), lib.PairLimiter(limit=100)))
    return nc


def hdr(**kw):
    base = {"From": "claude@project-h", "From-Pid": "999", "Reply-Inbox": "_INBOX.x", "Nats-Msg-Id": os.urandom(8).hex(), "Sent-At": str(int(time.time()))}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


PLAY = S(100, "project-a", sid="aaaa11")
ALGO = S(200, "algorithm", sid="bbbb22")

# ---------------------------------------------------------------- 1. the address: agent@project#name
print("-- address syntax")
check("no name: unchanged", lib.parse_destination("claude@project-a", "x") == ("claude", "project-a", None, None))
check("local session", lib.parse_destination("claude@project-a#algorithm", "x") == ("claude", "project-a", None, "algorithm"))
check("the name is last, after the machine", lib.parse_destination("claude@project-a@mac-b#algorithm", "x", "mac-a") == ("claude", "project-a", "mac-b", "algorithm"))
check("my own project on another Mac, with a name", lib.parse_destination("claude@@mac-b#algorithm", "mine", "mac-a") == ("claude", "mine", "mac-b", "algorithm"))
check("the machine that is this Mac counts as local", lib.parse_destination("claude@project-a@mac-a#algorithm", "x", "mac-a") == ("claude", "project-a", None, "algorithm"))
check("a name is case-insensitive and cleaned like a project name", lib.parse_destination("claude@project-a#My_Session 2", "x")[3] == "my-session-2")
check("bare agent with a name uses my project", lib.parse_destination("codex#bohr", "mine") == ("codex", "mine", None, "bohr"))
raises("a # with no name is an error, never 'any session'", lambda: lib.parse_destination("claude@project-a#", "x"), "bad session name")
raises("two # are an error", lambda: lib.parse_destination("claude@project-a#a#b", "x"), "bad session name")
raises("an @ inside the name is an error", lambda: lib.parse_destination("claude@project-a#a@mac-b", "x"), "bad session name")
raises("only symbols after the # is an error", lambda: lib.parse_destination("claude@project-a#!!!", "x"), "bad session name")
raises("a name over 64 characters is an error", lambda: lib.parse_destination("claude@p#" + "a" * 65, "x"), "bad session name")
raises("a name with a newline is an error", lambda: lib.parse_destination("claude@p#a\nb", "x"), "bad session name")
raises("a bad agent part is still an error", lambda: lib.parse_destination("@p#a", "x"), "bad address")
raises("the old parse_target refuses a # (old callers can never misroute a named address)", lambda: lib.parse_target("claude@p#a", "x"), "bad address")
raises("parse_address refuses a # too", lambda: lib.parse_address("claude@p#a", "x"), "bad address")
check("shell characters cannot survive cleaning", lib.valid_session("a;rm -rf $HOME") == "a-rm-rf-home")
check("valid_session: unusable inputs are None", [lib.valid_session(x) for x in ("", None, "   ", "!!!", "a@b", "a#b", "x" * 65)] == [None] * 7)

# ---------------------------------------------------------------- 2. where a session's name comes from
print("-- session names")
sess_dir = os.environ["POSTBOX_CLAUDE_SESSIONS_DIR"]
me_pid = os.getpid()
rec = {"pid": me_pid, "sessionId": "9d2b15aa-1111-4222-8333-444455556666", "name": "Algorithm", "nameSource": "user", "startedAt": int(time.time() * 1000) - 5000, "cwd": "/x"}
path = os.path.join(sess_dir, f"{me_pid}.json")
open(path, "w").write(json.dumps(rec))
check("Claude's session file gives the name (cleaned) and a short id", lib.session_identity("claude", me_pid, None) == ("algorithm", "556666"), str(lib.session_identity("claude", me_pid, None)))
rec["name"] = "Renamed Live"
open(path, "w").write(json.dumps(rec))
check("a /rename applies at once (the file is read on every look)", lib.session_identity("claude", me_pid, None)[0] == "renamed-live")
rec["startedAt"] = int(time.time() * 1000) - 7 * 86400 * 1000
open(path, "w").write(json.dumps(rec))
check("a file left by an earlier process with the same number is ignored", lib.session_identity("claude", me_pid, None) == ("", ""))
open(path, "w").write("{not json")
check("a damaged file gives no name and no crash", lib.session_identity("claude", me_pid, None) == ("", ""))
open(path, "w").write(json.dumps(dict(rec, pid=1, startedAt=int(time.time() * 1000))))
check("a file that names another pid is ignored", lib.session_identity("claude", me_pid, None) == ("", ""))
open(path, "w").write(json.dumps(dict(rec, startedAt=int(time.time() * 1000), name="")))
check("no name in the file: the short id is the name", lib.session_identity("claude", me_pid, None) == ("", "556666") and lib.sender_session(("claude", "p", me_pid)) == "556666")
open(path, "w").write(json.dumps(dict(rec, startedAt=int(time.time() * 1000), name="project-h-9c")))
check("the sender's own name comes from the same file", lib.sender_session(("claude", "p", me_pid)) == "project-h-9c")
check("a sender with no process (--as) has no session name", lib.sender_session(("claude", "p", None)) is None and lib.sender_session(("grok", "p", me_pid)) is None)
check("a Session shows its name in its label and answers to name, short id and handle",
      PLAY.label == "claude@project-a#project-a (pid 100)" and PLAY.matches("project-a") and PLAY.matches("aaaa11") and not PLAY.matches("algorithm"))
check("a session with neither name nor id keeps the old label (and is reached as p<pid>)", S(7, "", sid="").label == "claude@project-a (pid 7)" and S(7, "", sid="").handle == "p7")

# Codex: the name column of its thread table
db = os.path.join(TMP, "state_5.sqlite")
con = sqlite3.connect(db)
con.execute("create table threads (id text primary key, name text, cwd text, archived integer default 0)")
THREAD = "01a00000-0000-7000-8000-000000000a04"
con.execute("insert into threads (id, name, cwd) values (?, ?, ?)", (THREAD, "Read Music Date handoff", "/x"))
con.execute("insert into threads (id, name, cwd) values (?, ?, ?)", ("01a00000-0000-7000-8000-0000003f385f", None, "/x"))
con.commit()
con.close()
real_db = lib.codex_state_db
lib.codex_state_db = lambda: db
check("Codex: the thread table's name column is the session name", lib.codex_thread_name(THREAD) == "read-music-date-handoff")
check("Codex: a thread with no name gives '' and the short id is used", lib.codex_thread_name("01a00000-0000-7000-8000-0000003f385f") == ""
      and lib.session_identity("codex", 5, "01a00000-0000-7000-8000-0000003f385f") == ("", "3f385f"))
check("Codex: an unknown thread id gives nothing", lib.session_identity("codex", 5, "01a00000-0000-7000-8000-000000000000")[0] == "")
lib.codex_state_db = lambda: None
check("Codex: no database at all gives no name, the short id, and no crash", lib.session_identity("codex", 5, THREAD) == ("", "000a04") and lib.codex_thread_name(THREAD) == "")
lib.codex_state_db = real_db

# ---------------------------------------------------------------- 3. exactly one session: delivered, with the sender's name in the reply command
print("-- one session")
door = Door([PLAY])
nc = handle(Msg("post.project-a.claude", "build is green", hdr(**{"From-Session": "project-h-9c"})), {"claude": door})
check("one open session: delivered", nc.answer["status"] == "delivered" and len(door.got) == 1 and door.got[0][0] == 100, str(nc.answer))
check("the receiver sees the sender as agent@project#name", door.got[0][1].startswith("Message from claude@project-h#project-h-9c (post box):"), door.got[0][1][:120])
check("and the reply command it prints targets that one session", 'post claude@project-h#project-h-9c "your reply"' in door.got[0][1])
door = Door([PLAY])
handle(Msg("post.project-a.claude", "hello", hdr()), {"claude": door})
check("a sender with no session name looks exactly as before", door.got[0][1].startswith("Message from claude@project-h (post box):") and 'post claude@project-h "your reply"' in door.got[0][1])
door = Door([PLAY])
handle(Msg("post.project-a.claude", "hi", hdr(**{"From": "claude@dotclaude@mac-b", "From-Session": "Gamma-A1"})), {"claude": door})
check("cross-Mac sender: the reply command carries machine THEN name (agent@project@machine#name)",
      door.got[0][1].startswith("Message from claude@dotclaude@mac-b#gamma-a1 (post box):") and 'post claude@dotclaude@mac-b#gamma-a1 "your reply"' in door.got[0][1])
door = Door([PLAY])
handle(Msg("post.project-a.claude", "hi", hdr(**{"From-Session": "x; rm -rf /"})), {"claude": door})
check("a hostile sender session name is cleaned before it reaches a reply command", "#x-rm-rf" in door.got[0][1] and ";" not in door.got[0][1].split("\n")[0])

# ---------------------------------------------------------------- 4. a name: only that session
print("-- named")
door = Door([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", "to the algorithm one", hdr(**{"To-Session": "algorithm"})), {"claude": door})
check("named: delivered ONLY to the session with that name", nc.answer["status"] == "delivered" and [p for p, _ in door.got] == [200], str(door.got))
check("the answer names the session it went to", "claude@project-a#algorithm (pid 200)" in nc.answer["detail"], nc.answer["detail"])
door = Door([PLAY, ALGO])
handle(Msg("post.project-a.claude", "x", hdr(**{"To-Session": "PROJECT-A"})), {"claude": door})
check("the name is case-insensitive", [p for p, _ in door.got] == [100])
door = Door([PLAY, ALGO])
handle(Msg("post.project-a.claude", "x", hdr(**{"To-Session": "bbbb22"})), {"claude": door})
check("the short id reaches the same session", [p for p, _ in door.got] == [200])
door = Door([PLAY, ALGO])
msg = Msg("post.project-a.claude", "x", hdr(**{"To-Session": "nosuch"}))
nc = handle(msg, {"claude": door})
check("unknown name with sessions open: refused, nobody gets it", nc.answer["status"] == "refused" and door.got == [] and msg.termed, str(nc.answer))
check("the refusal lists the current names", "algorithm" in nc.answer["detail"] and "project-a" in nc.answer["detail"] and "nosuch" in nc.answer["detail"], nc.answer["detail"])
door = Door([])
msg = Msg("post.project-a.claude", "x", hdr(**{"To-Session": "algorithm"}))
nc = handle(msg, {"claude": door})
check("a name when NOBODY is open: queued as before (7 days), not refused", nc.answer["status"] == "queued" and msg.naked == 10 and "7 days" in nc.answer["detail"], str(nc.answer))
door = Door([PLAY, ALGO])
msg = Msg("post.project-a.claude", "x", hdr(**{"To-Session": "bad name!"}))
door2 = Door([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", "x", hdr(**{"To-Session": "!!!"})), {"claude": door2})
check("an unusable name in the header is refused in words", nc.answer["status"] == "refused" and door2.got == [] and "not a usable session name" in nc.answer["detail"], str(nc.answer))
door = Door([PLAY, ALGO])
handle(Msg("post.project-a.claude", "x", hdr(**{"To-Session": "algorithm", "From-Pid": "200"})), {"claude": door})
check("the sender's own session is never the target (it is excluded by pid)", door.got == [])

# ---------------------------------------------------------------- 5. several sessions, no name: hold + bounce
print("-- several sessions")
door = Door([PLAY, ALGO])
msg = Msg("post.project-a.claude", "please re-run the tests", hdr(**{"From-Session": "project-h-9c"}))
nc = handle(msg, {"claude": door})
ans = nc.answer
check("two sessions, no name: NOT delivered to anyone", door.got == [], str(door.got))
check("the sender gets a normal result: status held", ans["status"] == "held" and ans.get("reason") == "several_sessions", str(ans))
check("the stream message is acked (the hold owns it now), not redelivered every 10 s", msg.acked and not msg.naked and not msg.termed)
hid = [h["id"] for h in lib.hold_list()][0]
check("the answer carries the hold id, both names and a ready command for each",
      hid in ans["detail"] and f"post pick {hid} claude@project-a#project-a" in ans["detail"] and f"post pick {hid} claude@project-a#algorithm" in ans["detail"], ans["detail"])
check("it says nothing was delivered and the message is kept 7 days", "Nothing was delivered" in ans["detail"] and "7 days" in ans["detail"])
held = lib.hold_get(hid)
check("the held message keeps the full text, its sender and the sender's session", held["text"] == "please re-run the tests" and held["frm"] == "claude@project-h"
      and held["session"] == "project-h-9c" and held["agent"] == "claude" and held["project"] == "project-a")
mode = oct(os.stat(os.path.join(os.environ["POSTBOX_HELD_DIR"], f"{hid}.json")).st_mode & 0o777)
check("held files are private (mode 600)", mode == "0o600", mode)

# pick it
door = Door([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", f"pick {hid}", hdr(**{"Pick": hid, "To-Session": "algorithm", "From-Session": "project-h-9c"})), {"claude": door})
check("pick: delivered to the chosen session only", nc.answer["status"] == "delivered" and [p for p, _ in door.got] == [200], str(nc.answer) + str(door.got))
check("pick: the receiver reads the ORIGINAL text and the original sender, not 'pick <id>'",
      "please re-run the tests" in door.got[0][1] and door.got[0][1].startswith("Message from claude@project-h#project-h-9c (post box):") and "pick " + hid not in door.got[0][1], door.got[0][1][:160])
check("pick: no stale banner even though a person just chose (the pick is a fresh confirmation)", "STALE MAIL" not in door.got[0][1])
check("pick: the hold is gone after it was sent", lib.hold_get(hid) is None and hid in nc.answer["detail"])
nc = handle(Msg("post.project-a.claude", f"pick {hid}", hdr(**{"Pick": hid, "To-Session": "algorithm"})), {"claude": Door([PLAY, ALGO])})
check("pick twice: the second is refused (no double delivery)", nc.answer["status"] == "refused" and nc.answer["reason"] == "pick_unknown", str(nc.answer))

# more pick rules
msg = Msg("post.project-a.claude", "second one", hdr())
handle(msg, {"claude": Door([PLAY, ALGO])})
hid2 = [h["id"] for h in lib.hold_list()][0]
door = Door([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": hid2, "To-Session": "nosuch"})), {"claude": door})
check("pick to a name nobody has: refused, nothing delivered, the message STAYS held", nc.answer["status"] == "refused" and door.got == [] and lib.hold_get(hid2) is not None, str(nc.answer))
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": hid2, "To-Session": "algorithm", "From": "claude@someone-else"})), {"claude": door})
check("pick by a different sender is refused (only the sender can send a held message on)", nc.answer["reason"] == "pick_not_yours" and door.got == [] and lib.hold_get(hid2) is not None, str(nc.answer))
nc = handle(Msg("post.other.claude", "pick", hdr(**{"Pick": hid2, "To-Session": "algorithm"})), {"claude": Door([S(300, "algorithm", project="other")])})
check("pick into a different project than the held message was for is refused", nc.answer["reason"] == "pick_mismatch" and lib.hold_get(hid2) is not None, str(nc.answer))
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": hid2})), {"claude": door})
check("pick without a session is refused in words", nc.answer["reason"] == "pick_no_session" and lib.hold_get(hid2) is not None, str(nc.answer))
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": "zzzzzz", "To-Session": "algorithm"})), {"claude": door})
check("pick with a malformed id is refused", nc.answer["reason"] == "pick_unknown" and door.got == [])
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": hid2, "To-Session": "algorithm"})), {"claude": Door([])})
check("pick when nobody is open: refused, the hold stays", nc.answer["reason"] == "pick_nobody" and lib.hold_get(hid2) is not None, str(nc.answer))


class BrokenDoor(Door):
    def deliver(self, session, text, timeout=3.0): raise OSError("socket gone")


msg = Msg("post.project-a.claude", "pick", hdr(**{"Pick": hid2, "To-Session": "algorithm"}))
nc = handle(msg, {"claude": BrokenDoor([PLAY, ALGO])})
check("pick that cannot be handed over: refused, hold kept, not retried in the background", nc.answer["status"] == "refused" and msg.termed and not msg.naked and lib.hold_get(hid2) is not None, str(nc.answer))
door = Door([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": hid2, "To-Session": "project-a"})), {"claude": door})
check("pick after the earlier failures still works and sends the second held message", nc.answer["status"] == "delivered" and "second one" in door.got[0][1] and lib.hold_get(hid2) is None)

# two sessions with the SAME name
TWIN_A, TWIN_B = S(300, "algorithm", sid="cccc33"), S(301, "algorithm", sid="dddd44")
door = Door([TWIN_A, TWIN_B, PLAY])
nc = handle(Msg("post.project-a.claude", "which twin", hdr(**{"To-Session": "algorithm"})), {"claude": door})
check("two sessions with the same name: held as ambiguous, not delivered to either", nc.answer["status"] == "held" and nc.answer["reason"] == "ambiguous_name" and door.got == [], str(nc.answer))
tw = [h["id"] for h in lib.hold_list()][0]
check("the bounce tells them apart by short id", f"post pick {tw} claude@project-a#cccc33" in nc.answer["detail"] and f"post pick {tw} claude@project-a#dddd44" in nc.answer["detail"], nc.answer["detail"])
nc = handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": tw, "To-Session": "dddd44"})), {"claude": door})
check("picking by short id reaches exactly that twin", nc.answer["status"] == "delivered" and [p for p, _ in door.got] == [301], str(door.got))

# a sender on another Mac gets commands that name the machine
door = Door([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", "from mac-b", hdr(**{"From": "claude@dotclaude@mac-b", "From-Session": "anc"})), {"claude": door})
ph = [h["id"] for h in lib.hold_list()][0]
check("cross-Mac sender: the bounce commands are agent@project@THIS-MAC#name (so they route back here)", f"post pick {ph} claude@project-a@mac-a#algorithm" in nc.answer["detail"], nc.answer["detail"])
nc = handle(Msg("post.project-a.claude", f"pick {ph}", hdr(**{"From": "claude@dotclaude@mac-b", "Pick": ph, "To-Session": "algorithm"})), {"claude": door})
check("cross-Mac pick by the same sender address is accepted", nc.answer["status"] == "delivered" and "from mac-b" in door.got[0][1] and door.got[0][1].startswith("Message from claude@dotclaude@mac-b#anc"), str(nc.answer))

# ---------------------------------------------------------------- 6. mail that waited, then finds several sessions
print("-- queued mail that later finds several sessions")
door = Door([PLAY, ALGO])
SENDER = S(900, "project-h-9c", project="project-h", sid="eeee55")
sender_door = Door([SENDER])
doors = {"claude": Door([PLAY, ALGO, SENDER])}
doors["claude"].got = []
msg = Msg("post.project-a.claude", "waited a while", hdr(**{"From": "claude@project-h", "From-Pid": "900", "From-Session": "project-h-9c"}), delivered=2)
nc = handle(msg, doors)
check("held (not delivered) when redelivered mail finds several sessions", nc.answer["status"] == "held" and msg.acked and [p for p, _ in doors["claude"].got] == [900], str(doors["claude"].got))
notice = doors["claude"].got[0][1]
check("the sender's own session is told by a message (its command ended long ago): the same bounce and ready commands", "Post box notice" in notice and "post pick " in notice
      and "claude@project-a#algorithm" in notice, notice)
check("the notice went to the sender's session only (found by name), not to the other sessions in its project", len(doors["claude"].got) == 1)
doors = {"claude": Door([PLAY, ALGO])}
msg = Msg("post.project-a.claude", "forwarded", hdr(**{"From": "claude@dotclaude@mac-b", "From-Session": "anc", "Forwarded": "1"}))
nc = handle(msg, doors)
check("mail from another Mac that came through the forwarder: bounced AND a notice is forwarded back to that Mac's session",
      nc.answer["status"] == "held" and len(nc.published) == 1 and nc.published[0][0] == "forward.mac-b.dotclaude.claude" and nc.published[0][2].get("To-Session") == "anc"
      and "post pick " in nc.published[0][1] and "@mac-a#" in nc.published[0][1], str(nc.published))
nc = handle(Msg("post.project-a.claude", "live, not late", hdr(**{"From": "claude@dotclaude@mac-b"})), {"claude": Door([PLAY, ALGO])})
check("a live send needs no notice (the sender reads the answer)", nc.published == [])
nc = handle(Msg("post.project-a.claude", "unknown late", hdr(**{"To-Session": "nosuch", "From-Session": "project-h-9c", "From-Pid": "900"}), delivered=3), {"claude": Door([PLAY, ALGO, SENDER])})
check("a late unknown-name refusal is also told to the sender's session", nc.answer["status"] == "refused")

# ---------------------------------------------------------------- 7. the other doors are unchanged
print("-- other doors")
gdoor = InboxDoor([S(5, "", project="main-desk", sid="", agent="grok")])
nc = handle(Msg("post.main-desk.grok", "for the inbox", hdr(**{"To-Session": "whatever"})), {"grok": gdoor})
check("grok: one inbox per project, a #name is not used and the message is delivered", nc.answer["status"] == "delivered" and len(gdoor.got) == 1 and "was not used" in nc.answer["detail"], str(nc.answer))
gdoor = InboxDoor([S(5, "", project="main-desk", sid="", agent="grok"), S(6, "", project="main-desk", sid="", agent="grok")])
nc = handle(Msg("post.main-desk.grok", "two groks", hdr()), {"grok": gdoor})
check("grok: several sessions still means one inbox delivery, never a bounce", nc.answer["status"] == "delivered" and len(gdoor.got) == 1)
gbd = lib.DOORS["grokbot-desktop"]
check("grokbot-desktop: no per-session door", not getattr(gbd, "per_session", False) and getattr(lib.DOORS["claude"], "per_session", False) and getattr(lib.DOORS["codex"], "per_session", False))


class OldStyleDoor:   # the shape the older tests use: no per_session attribute
    def __init__(self, targets): self.targets, self.delivered = targets, []
    def resolve(self, project, exclude_pid=None): return self.targets
    def deliver(self, t, text): self.delivered.append(text)


od = OldStyleDoor([PLAY, ALGO])
nc = handle(Msg("post.project-a.claude", "legacy", hdr()), {"claude": od})
check("a door that does not declare per_session keeps delivering to every match (nothing else changes)", len(od.delivered) == 2 and nc.answer["status"] == "delivered")
# Codex rides the same rule
cdoor = lib.CodexDoor()
cdoor.deliver = lambda session, text, timeout=25.0: DELIVERED.append((session.pid, text))
DELIVERED = []
cdoor.sessions = lambda: [S(11, "fix-bug", agent="codex", sid="111111"), S(12, "review", agent="codex", sid="222222")]
nc = handle(Msg("post.project-a.codex", "codex mail", hdr()), {"codex": cdoor})
check("codex: several screens and no name is held too", nc.answer["status"] == "held" and DELIVERED == [] and "codex@project-a#fix-bug" in nc.answer["detail"], str(nc.answer))
nc = handle(Msg("post.project-a.codex", "codex mail", hdr(**{"To-Session": "review"})), {"codex": cdoor})
check("codex: a name reaches that thread's screen only", nc.answer["status"] == "delivered" and [p for p, _ in DELIVERED] == [12], str(DELIVERED))

# ---------------------------------------------------------------- 8. the held store
print("-- held store")
now = time.time()
a = lib.hold_put({"msg_id": "m1", "agent": "claude", "project": "p", "frm": "claude@x", "text": "one", "sent_at": now})
b = lib.hold_put({"msg_id": "m1", "agent": "claude", "project": "p", "frm": "claude@x", "text": "one", "sent_at": now})
check("the same stream message held twice (a redelivery after a crash) is one hold", a == b and len(a) == 6)
old = lib.hold_put({"msg_id": "old", "agent": "claude", "project": "p", "frm": "claude@x", "text": "old", "sent_at": now - lib.MAX_AGE_S - 10})
check("a hold lives 7 days from when the message was SENT: an older one is gone on first look", lib.hold_get(old) is None)
mid = lib.hold_put({"msg_id": "mid", "agent": "claude", "project": "p", "frm": "claude@x", "text": "mid", "sent_at": now - lib.MAX_AGE_S + 500})
check("one with time left is still there", lib.hold_get(mid) is not None and lib.hold_get(mid, now=now + 600) is None)
lib.hold_put({"msg_id": "m3", "agent": "claude", "project": "p", "frm": "claude@x", "text": "three", "sent_at": now - lib.MAX_AGE_S - 5})
check("the relay's sweep removes expired holds", lib.hold_sweep() >= 0 and all(h["text"] != "three" for h in lib.hold_list()))
check("an id that is not hex is never read", lib.hold_get("../../etc/passwd") is None and lib.hold_get("") is None and lib.hold_delete("../x") is False)
saved = lib.HOLD_MAX
lib.HOLD_MAX = len(lib.hold_list())
try:
    lib.hold_put({"msg_id": "overflow", "agent": "claude", "project": "p", "frm": "claude@x", "text": "x", "sent_at": now})
    check("a full hold store refuses more", False)
except lib.HoldFull:
    check("a full hold store refuses more (HoldFull)", True)
msg = Msg("post.project-a.claude", "too many", hdr())
nc = handle(msg, {"claude": Door([PLAY, ALGO])})
check("relay: when the store is full the sender is refused in words, with the direct form", nc.answer["status"] == "refused" and nc.answer["reason"] == "hold_full" and "#<name>" in nc.answer["detail"], str(nc.answer))
lib.HOLD_MAX = saved

# ---------------------------------------------------------------- 9. the post command
print("-- post command")
sent = []


async def fake_send(to_agent, to_project, to_machine, text, me):
    sent.append((to_agent, to_project, to_machine, text, dict(post.MAIL_EXTRA)))
    sys.exit(0)


real_send, real_resolve = post.send, lib.resolve_sender
post.send = fake_send
lib.resolve_sender = lambda flag=None: (("claude", "project-h", me_pid), "test")
lib.sender_session = lambda me: "project-h-9c"


def cli(argv):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            post.run(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
    return code, out.getvalue(), err.getvalue()


code, out, err = cli(["post", "claude@project-a#Algorithm", "hello", "there"])
check("post claude@project-a#name: sends to that project with To-Session and my own From-Session",
      sent and sent[-1][:4] == ("claude", "project-a", None, "hello there") and sent[-1][4].get("To-Session") == "algorithm" and sent[-1][4].get("From-Session") == "project-h-9c", str(sent))
code, out, err = cli(["post", "claude@project-a@mac-b#algorithm", "hello"])
check("post agent@project@machine#name: machine and name both parsed", sent[-1][:3] == ("claude", "project-a", "mac-b") and sent[-1][4].get("To-Session") == "algorithm", str(sent[-1]))
code, out, err = cli(["post", "claude@project-a", "no name"])
check("post without a name: no To-Session header", "To-Session" not in {k: v for k, v in sent[-1][4].items() if v})
code, out, err = cli(["post", "claude@project-a#", "x"])
check("post with an empty name: exit 2 and the reason in words, nothing sent", code == 2 and "bad session name" in err, err)
n = len(sent)
code, out, err = cli(["post", "claude@project-a#a#b", "x"])
check("post with two #: exit 2, nothing sent", code == 2 and len(sent) == n)
code, out, err = cli(["post", "pick", hid2])
check("post pick without a target: usage error, nothing sent", code == 2 and "post pick needs" in err and len(sent) == n, err)
code, out, err = cli(["post", "pick", "nothex", "claude@project-a#algorithm"])
check("post pick with a bad id: usage error", code == 2 and len(sent) == n)
code, out, err = cli(["post", "pick", "abc123", "claude@project-a"])
check("post pick to an address without #name: tells how to add it", code == 2 and "#<name>" in err and len(sent) == n, err)
code, out, err = cli(["post", "pick", "ABC123", "claude@project-a@mac-b#algorithm"])
check("post pick <id> <address>: sends the Pick header (id lower-cased) and the session",
      sent[-1][:3] == ("claude", "project-a", "mac-b") and sent[-1][4].get("Pick") == "abc123" and sent[-1][4].get("To-Session") == "algorithm" and sent[-1][3] == "pick abc123", str(sent[-1]))

# held listing
lib.hold_put({"msg_id": "listme", "agent": "claude", "project": "project-a", "frm": "claude@project-h", "session": "project-h-9c", "text": "list me please", "sent_at": time.time()})
real_resolve_door = lib.DOORS["claude"].resolve
lib.DOORS["claude"].resolve = lambda project, exclude_pid=None, sessions=None: [PLAY, ALGO] if project == "project-a" else []
code, out, err = cli(["post", "held"])
lib.DOORS["claude"].resolve = real_resolve_door
check("post held lists id, destination, sender with its session, age, a preview and a ready command per open session",
      code == 0 and "claude@project-a" in out and "claude@project-h#project-h-9c" in out and "list me please" in out and "claude@project-a#algorithm" in out and "post pick " in out, out)
check("post held with extra words is a usage error", cli(["post", "held", "x"])[0] == 2)

# who shows names
lib.all_sessions = lambda: [PLAY, ALGO]
lib.inbox_only_agents = lambda: []
code, out, err = cli(["post", "who"])
check("post who shows each session's name after the project", "#project-a" in out and "#algorithm" in out and "pid 200" in out, out)

post.send, lib.resolve_sender = real_send, real_resolve

# the real send(): a 'held' answer is a normal result line and its own exit code


class SubMsg:
    def __init__(self, body): self.data = json.dumps(body).encode()


class FakeSub:
    def __init__(self, body): self.body = body
    async def next_msg(self, timeout=None): return SubMsg(self.body)


class FakeJS:
    def __init__(self, nc): self.nc = nc
    async def publish(self, subject, data, headers=None, timeout=None): self.nc.published.append((subject, data.decode(), dict(headers or {})))


class FakeNC:
    def __init__(self, body): self.body, self.published = body, []
    def new_inbox(self): return "_INBOX.fake"
    async def subscribe(self, inbox): return FakeSub(self.body)
    def jetstream(self): return FakeJS(self)
    async def close(self): pass


body = {"status": "held", "detail": "2 claude sessions are open in project-a and the message did not say which one. Nothing was delivered.\n  post pick a1b2c3 claude@project-a#algorithm", "targets": ["a", "b"], "reason": "several_sessions"}
fake = FakeNC(body)


async def fake_connect(target, timeout=3): return fake


real_connect, real_local = lib.connect_fast, lib.local_target
lib.connect_fast, lib.local_target = fake_connect, lambda: lib.Target("nats://x", None, "this Mac")
post.MAIL_EXTRA.clear()
post.MAIL_EXTRA.update({"From-Session": "project-h-9c", "To-Session": None, "Pick": None})
out = io.StringIO()
code = None
with contextlib.redirect_stdout(out):
    try:
        post.CMD.clear()
        run(post.send("claude", "project-a", None, "hello", ("claude", "project-h", 55)))
    except SystemExit as e:
        code = e.code
lib.connect_fast, lib.local_target = real_connect, real_local
printed = out.getvalue()
check("a held answer is printed as a normal result line: 'held: ...' with the ready commands", printed.startswith("held: 2 claude sessions") and "post pick a1b2c3 claude@project-a#algorithm" in printed, printed)
check("a held answer exits 5 (not 0 delivered, not 4 refused), and telemetry records it as held", code == 5 and post.CMD.get("result") == "held", f"{code} {post.CMD.get('result')}")
hdrs = fake.published[0][2]
check("the message goes out with From-Session; no To-Session header when none was given", hdrs.get("From-Session") == "project-h-9c" and "To-Session" not in hdrs and hdrs["From"] == "claude@project-h", str(hdrs))

# ---------------------------------------------------------------- 10. telemetry: the new decisions, metadata only
print("-- telemetry")
telemetry.reset()
os.environ["POSTBOX_TELEMETRY"] = "1"
spool = os.environ["POSTBOX_TELEMETRY_SPOOL"]
for f in os.listdir(spool) if os.path.isdir(spool) else []:
    os.unlink(os.path.join(spool, f))
SECRET = "SECRET-BODY-TEXT-12345"
handle(Msg("post.project-a.claude", SECRET, hdr()), {"claude": Door([PLAY])})                    # delivered_one
handle(Msg("post.project-a.claude", SECRET, hdr(**{"To-Session": "algorithm"})), {"claude": Door([PLAY, ALGO])})  # delivered_named
handle(Msg("post.project-a.claude", SECRET + "2", hdr()), {"claude": Door([PLAY, ALGO])})         # bounced_several
tid = [h for h in lib.hold_list() if h["text"] == SECRET + "2"][0]["id"]
handle(Msg("post.project-a.claude", "pick", hdr(**{"Pick": tid, "To-Session": "algorithm"})), {"claude": Door([PLAY, ALGO])})  # picked
handle(Msg("post.project-a.claude", SECRET, hdr(**{"To-Session": "nosuch"})), {"claude": Door([PLAY, ALGO])})   # refused_unknown_session
events = []
for f in sorted(os.listdir(spool)):
    events.append(json.loads(open(os.path.join(spool, f)).read())["payload"]["metadata"])
routes = [e.get("route") for e in events]
check("telemetry: delivered_one, delivered_named, bounced_several, picked and the unknown-name refusal are all recorded",
      sorted(routes) == sorted(["delivered_one", "delivered_named", "bounced_several", "picked", "refused_unknown_session"]), str(routes))
byroute = {e["route"]: e for e in events}
check("telemetry: the bounce is result held with a reason and the number of open sessions", byroute["bounced_several"]["result"] == "held" and byroute["bounced_several"]["reason"] == "several_sessions"
      and byroute["bounced_several"]["sessions_n"] == 2, str(byroute["bounced_several"]))
check("telemetry: the pick is marked picked and delivered", byroute["picked"]["result"] == "delivered" and byroute["picked"]["picked"] is True)
check("telemetry: metadata only, the message text is in no event", SECRET not in json.dumps(events), "text leaked")
os.environ["POSTBOX_TELEMETRY"] = "0"

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
