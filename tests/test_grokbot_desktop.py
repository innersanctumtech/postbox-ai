#!/usr/bin/env python3
"""Offline tests for the grokbot-desktop door (the agents inside Grok Bot desktop, NOT the grok command line tool):
mail to grokbot-desktop@<project> is always delivered into its inbox, `post inbox` reads and clears it when the sender states who it is,
it can send with --as, grok's own inbox is never touched, and `post who` lists it only once its inbox exists.
No real mailbox, no real app, no message leaves this process.
Run: .venv/bin/python tests/test_grokbot_desktop.py"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool
import sys
import tempfile
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib  # noqa: E402
import post as post_cli  # noqa: E402
import relay  # noqa: E402

for _k in list(os.environ):
    if _k.startswith(("CLAUDE", "CODEX", "GROK", "POSTBOX_")):
        del os.environ[_k]
TMP = tempfile.mkdtemp()
os.environ["POSTBOX_MACHINE"] = "mac-a-test"
os.environ["POSTBOX_SUPPORT_DIR"] = os.path.join(TMP, "support")
os.environ["POSTBOX_INBOX_DIR"] = os.path.join(TMP, "inbox")
PASS = FAIL = 0
run = asyncio.run
GBD = "grokbot-desktop"


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def cli(argv):
    out, err, code = io.StringIO(), io.StringIO(), None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            post_cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


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
GROK_SESSION = lib.Session("grok", 75656, "/home/x/main-desk", "main-desk", "sid")


def deliver(subject, text="hello main-desk", hdr=HDR, delivered=1, grok_sessions=()):
    m, nc = FakeMsg(subject, text, dict(hdr), delivered), FakeNC()
    with mock.patch.object(lib, "grok_sessions", lambda: list(grok_sessions)):
        run(relay.handle(nc, m, lib.DOORS, lib.PairLimiter()))
    return m, nc


def inbox_texts(agent, project):
    return [p.read_text() for p in lib.inbox_list(agent, project)]


# ------------------------------------------------------------------ the door
print("the door")
check("grokbot-desktop has a door of its own, next to grok's", isinstance(lib.DOORS[GBD], lib.GrokbotDesktopDoor) and isinstance(lib.DOORS["grok"], lib.GrokDoor))
t = lib.DOORS[GBD].resolve("Main-Desk")
check("resolve gives exactly one synthetic target for the project, with no session discovery", len(t) == 1 and t[0].project == "main-desk" and t[0].agent == GBD, str(t))
check("and it answers the same whatever else is open (no process, no session list involved)", lib.DOORS[GBD].resolve("main-desk", exclude_pid=11, sessions=["ignored"]) == t)
check("the target is labelled as an inbox, not a process", t[0].label == "grokbot-desktop@main-desk (inbox)", t[0].label)

# ------------------------------------------------------------------ a message to grokbot-desktop
print("send to grokbot-desktop@main-desk")
m, nc = deliver("post.main-desk.grokbot-desktop")
detail = nc.sent[0][1]["detail"]
check("acked and the sender is told delivered, never queued (nobody-reachable cannot happen)", m.acked and not m.naked and nc.sent[0][1]["status"] == "delivered", str(nc.sent))
check("the answer carries the inbox note: it cannot be woken and how it reads this",
      detail.startswith("delivered to grokbot-desktop@main-desk (inbox)") and "cannot be woken" in detail
      and "POSTBOX_AS=grokbot-desktop@main-desk post inbox" in detail and "when its next turn starts" in detail, detail)
body = inbox_texts(GBD, "main-desk")
check("the message is waiting in the grokbot-desktop@main-desk inbox", len(body) == 1 and "Message from claude@claude (post box):\nhello main-desk" in body[0], str(body))
check("what it reads tells it to answer with --as and to read with POSTBOX_AS (it cannot be detected)",
      'post --as grokbot-desktop@main-desk claude@claude "your reply"' in body[0] and "run `POSTBOX_AS=grokbot-desktop@main-desk post inbox`" in body[0], body[0])

m, nc = deliver("post.other.grokbot-desktop", text="for another project")
check("another project's inbox is separate", nc.sent[0][1]["status"] == "delivered" and len(inbox_texts(GBD, "other")) == 1 and "another project" not in " ".join(inbox_texts(GBD, "main-desk")))

hdr_stale = {**HDR, "Sent-At": str(int(__import__("time").time()) - 7200)}
m, nc = deliver("post.main-desk.grokbot-desktop", text="old news", hdr=hdr_stale)
stale = [b for b in inbox_texts(GBD, "main-desk") if "old news" in b]
check("stale mail asks the sender with --as too, so the question can be sent from this agent", stale and stale[0].startswith("STALE MAIL") and 'post --as grokbot-desktop@main-desk claude@claude "Still relevant?' in stale[0], str(stale))

# ------------------------------------------------------------------ grok is untouched
print("grok and grokbot-desktop stay separate")
before = set(os.listdir(lib.inbox_root()))
m, nc = deliver("post.main-desk.grok", text="for the grok cli", grok_sessions=(GROK_SESSION,))
check("a message to grok@main-desk lands in grok's inbox only", nc.sent[0][1]["status"] == "delivered" and any("for the grok cli" in b for b in inbox_texts("grok", "main-desk"))
      and not any("for the grok cli" in b for b in inbox_texts(GBD, "main-desk")))
check("grok's answer and wording are unchanged (pid label, post inbox, no --as)",
      nc.sent[0][1]["detail"].startswith("delivered to grok@main-desk (pid 75656)") and "post inbox" in nc.sent[0][1]["detail"] and "POSTBOX_AS" not in nc.sent[0][1]["detail"], nc.sent[0][1]["detail"])
check("and what grok reads has no --as (it is detected)", all("--as" not in b for b in inbox_texts("grok", "main-desk")))
m, nc = deliver("post.main-desk.grok", text="no grok open", grok_sessions=())
check("grok with no live session is still queued (the new door did not change that)", nc.sent[0][1]["status"] == "queued" and m.naked == 10)

# ------------------------------------------------------------------ post inbox as grokbot-desktop
print("post inbox as grokbot-desktop@main-desk")
os.environ["POSTBOX_AS"] = f"{GBD}@main-desk"
with mock.patch.object(lib, "my_identity", return_value=None):
    code, out, err = cli(["post", "inbox"])
check("POSTBOX_AS=grokbot-desktop@main-desk post inbox prints its messages, oldest first", code is None and "hello main-desk" in out and "old news" in out and out.index("hello main-desk") < out.index("old news"), out + err)
check("it printed the grokbot-desktop messages and none of grok's", "for the grok cli" not in out and "No messages waiting" not in out)
check("and cleared them", inbox_texts(GBD, "main-desk") == [])
with mock.patch.object(lib, "my_identity", return_value=None):
    code, out, _ = cli(["post", "inbox"])
check("a second read says nothing is waiting", "No messages waiting for grokbot-desktop@main-desk" in out, out)
check("grok's inbox was not touched by that read", any("for the grok cli" in b for b in inbox_texts("grok", "main-desk")))
del os.environ["POSTBOX_AS"]
with mock.patch.object(lib, "my_identity", return_value=None):
    code, out, _ = cli(["post", "--as", "grok@main-desk", "inbox"])
check("the grok identity reads grok's inbox (unchanged) and clears only that", "for the grok cli" in out and inbox_texts("grok", "main-desk") == [])
with mock.patch.object(lib, "my_identity", return_value=None):
    deliver("post.main-desk.grokbot-desktop", text="via --as")
    code, out, _ = cli(["post", "--as", f"{GBD}@main-desk", "inbox"])
check("--as grokbot-desktop@main-desk works the same as POSTBOX_AS", "via --as" in out, out)

# ------------------------------------------------------------------ grokbot-desktop as the sender
print("send from grokbot-desktop")
sent = []


async def fake_send(to_agent, to_project, to_machine, text, me):
    sent.append((to_agent, to_project, to_machine, text, me))
    sys.exit(0)


with mock.patch.object(post_cli, "send", fake_send), mock.patch.object(lib, "my_identity", return_value=None):
    code, _, err = cli(["post", "--as", f"{GBD}@main-desk", "claude@claude", "hello", "from", "main-desk"])
    check("post --as grokbot-desktop@main-desk claude@claude sends as grokbot-desktop@main-desk, no process number",
          code == 0 and sent[-1] == ("claude", "claude", None, "hello from main-desk", (GBD, "main-desk", None)), f"{sent} {err}")
    code, out, _ = cli(["post", "--as", f"{GBD}@main-desk", "whoami"])
    check("whoami agrees", out.strip() == "grokbot-desktop@main-desk (the --as option)", out)
    os.environ["POSTBOX_AS"] = f"{GBD}@main-desk"
    cli(["post", "claude@claude", "via env"])
    check("POSTBOX_AS=grokbot-desktop@main-desk also sends as it", sent[-1][4] == (GBD, "main-desk", None), str(sent[-1]))
    del os.environ["POSTBOX_AS"]
    code, out, _ = cli(["post", "--as", f"{GBD}@main-desk", "grokbot-desktop@other", "to myself, other project"])
    check("it can address its own door too", code == 0 and sent[-1][:3] == (GBD, "other", None), str(sent[-1]))

cap = types.SimpleNamespace(headers=None)


class CaptureNC:
    def new_inbox(self):
        return "_INBOX.cap"

    async def subscribe(self, subject):
        class Sub:
            async def next_msg(self, timeout=None):
                return types.SimpleNamespace(data=json.dumps({"status": "delivered", "detail": "x"}).encode())
        return Sub()

    def jetstream(self):
        class JS:
            async def publish(self, subject, payload, headers=None, timeout=None):
                cap.headers = headers
        return JS()

    async def close(self):
        pass


async def fake_connect(target, timeout=3):
    return CaptureNC()


with mock.patch.object(lib, "connect_fast", fake_connect), contextlib.redirect_stdout(io.StringIO()):
    try:
        run(post_cli.send("claude", "claude", None, "hi", (GBD, "main-desk", None)))
    except SystemExit:
        pass
check("the mail header carries From grokbot-desktop@main-desk and no From-Pid", cap.headers and cap.headers["From"] == "grokbot-desktop@main-desk" and "From-Pid" not in cap.headers, str(cap.headers))
with mock.patch.object(lib, "claude_sessions", lambda: []), mock.patch.object(lib, "codex_sessions", lambda: []):   # never reach a real session
    m, nc = deliver("post.claude.claude", hdr={"From": "grokbot-desktop@main-desk", "Reply-Inbox": "_INBOX.x"})
check("the relay accepts a sender whose agent has no session and no door entry of its own kind (here: nobody home, so queued)",
      nc.sent and nc.sent[0][1]["status"] == "queued" and not m.termed, str(nc.sent))

# ------------------------------------------------------------------ post who
print("post who")
os.environ["POSTBOX_INBOX_DIR"] = os.path.join(TMP, "inbox-who")
with mock.patch.object(lib, "claude_sessions", lambda: []), mock.patch.object(lib, "codex_sessions", lambda: []), mock.patch.object(lib, "grok_sessions", lambda: []), \
        mock.patch.object(lib, "my_identity", return_value=None):
    code, out, _ = cli(["post", "who"])
    check("no inbox yet: grokbot-desktop is not listed (it cannot be discovered, so nothing is claimed)", "grokbot-desktop" not in out and "(none)" in out, out)
    deliver("post.main-desk.grokbot-desktop", text="one")
    deliver("post.main-desk.grokbot-desktop", text="two")
    code, out, _ = cli(["post", "who"])
    check("once its inbox exists: grokbot-desktop@main-desk  (inbox, 2 waiting; cannot be woken)", "grokbot-desktop@main-desk" in out and "(inbox, 2 waiting; cannot be woken)" in out and "(none)" not in out, out)
    cli(["post", "--as", f"{GBD}@main-desk", "inbox"])
    code, out, _ = cli(["post", "who"])
    check("after it reads, the line says 0 waiting", "(inbox, 0 waiting; cannot be woken)" in out, out)
    lib.inbox_put("grok", "main-desk", "grok mail")
    lib.inbox_put("claude", "main-desk", "claude mail")
    check("grok and claude inboxes are never listed as inbox-only agents", [a for a, _, _ in lib.inbox_only_agents()] == [GBD], str(lib.inbox_only_agents()))
    with mock.patch.object(lib, "grok_sessions", lambda: [GROK_SESSION]):
        code, out, _ = cli(["post", "who"])
    check("grok is still listed from its session list, with its pid, exactly as before", "grok@main-desk" in out and "pid 75656" in out, out)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
