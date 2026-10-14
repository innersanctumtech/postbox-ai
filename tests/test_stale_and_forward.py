#!/usr/bin/env python3
"""Offline tests for the 7-day queue, mail for a sleeping Mac (stored here, forwarded later) and the stale-mail banner.
Run: .venv/bin/python tests/test_stale_and_forward.py

Nothing here opens a real mailbox: the mailbox, the other Mac and the sender's identity are all fakes. Telemetry is switched off
(tests/test_telemetry.py covers the post_forward event and the stale fields).
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"
import sys
import time
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
os.environ["POSTBOX_MACHINE"] = "mac-a"
import lib  # noqa: E402
import post  # noqa: E402
import relay  # noqa: E402

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


# ---------------------------------------------------------------- 1. the queue lives 7 days, and says so in words
print("-- 7 days")
check("queued mail lives 7 days by default", lib.MAX_AGE_S == 7 * 24 * 3600 and lib.STALE_AFTER_S == 1800)
check("elapsed time reads plainly, days included", (lib.ago(5), lib.ago(240), lib.ago(3900), lib.ago(604800), lib.ago(86400), lib.ago(90000), lib.ago(2 * 86400 + 5 * 3600))
      == ("5 s", "4 min", "1 h 5 min", "7 days", "1 day", "1 day 1 h", "2 days 5 h"))


class Msg:
    def __init__(self, subject, text, headers, delivered=1, stored_ago=None, seq=1):
        self.subject, self.data, self.headers = subject, text.encode() if isinstance(text, str) else text, headers
        md = types.SimpleNamespace(num_delivered=delivered, sequence=types.SimpleNamespace(stream=seq, consumer=seq))
        if stored_ago is not None:
            from datetime import datetime, timedelta, timezone
            md.timestamp = datetime.now(timezone.utc) - timedelta(seconds=stored_ago)
        self.metadata = md
        self.acked = self.naked = self.termed = None

    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): self.termed = True


class NC:
    def __init__(self): self.sent = []
    async def publish(self, subj, data): self.sent.append((subj, json.loads(data)))
    async def flush(self, timeout=None): pass


class Door:
    def __init__(self, targets): self.targets, self.delivered = targets, []
    def resolve(self, project, exclude_pid=None): return self.targets
    def deliver(self, t, text): self.delivered.append(text)


T1 = lib.Session("claude", 11, "/a/p", "p", "/s/1")
HDR = {"From": "claude@myproj", "From-Pid": "1", "Reply-Inbox": "_INBOX.x"}


def handle(msg, door):
    nc = NC()
    with contextlib.redirect_stdout(io.StringIO()):
        run(relay.handle(nc, msg, {"claude": door}, lib.PairLimiter(), lib.PairLimiter(limit=100)))
    return nc


door = Door([])
nc = handle(Msg("post.p.claude", "hi", HDR), door)
detail = nc.sent[0][1]["detail"]
check("nobody there: the sender is told the REAL duration, not minutes", "kept for 7 days" in detail and "minutes" not in detail, detail)

# ---------------------------------------------------------------- 2. the age of a message: Sent-At first, queue age second, garbage ignored
print("-- age")
NOW = 1_800_000_000
check("Sent-At wins over the stream's queue age", relay.message_age_s({"Sent-At": str(NOW - 7200)}, 5.0, now=NOW) == 7200)
check("no Sent-At: the queue age is used", relay.message_age_s({}, 4000.5, now=NOW) == 4000.5 and relay.message_age_s(None, 12.0, now=NOW) == 12.0)
check("no Sent-At and no queue age: unknown", relay.message_age_s({}, None, now=NOW) is None)
for label, bad in (("text", "yesterday"), ("negative", "-5"), ("too short", "12345"), ("from 1970", "100000000"), ("from the future", str(NOW + 99999)), ("empty", ""), ("shell characters", "1800000000; rm -rf")):
    check(f"a garbage Sent-At is ignored ({label}): falls back to the queue age", relay.message_age_s({"Sent-At": bad}, 77.0, now=NOW) == 77.0)
check("a few minutes of clock difference is allowed (never a negative age)", relay.message_age_s({"Sent-At": str(NOW + 120)}, 9.0, now=NOW) == 0)

# ---------------------------------------------------------------- 3. the stale banner
print("-- stale banner")
plain = lib.render("claude@proj@mac-b", "run the tests")
stale = lib.render("claude@proj@mac-b", "run the tests", waited_s=5 * 3600 + 120)
check("fresh mail has no banner", "STALE MAIL" not in plain and plain.startswith("Message from claude@proj@mac-b (post box):"))
check("stale mail starts with the banner and says how long it waited", stale.startswith("STALE MAIL: this message waited 5 h 2 min in the post box before reaching you (sent "))
check("the banner names the exact command that asks the sender",
      'RUNNING: post claude@proj@mac-b "Still relevant? Your message from 5 h 2 min ago said: run the tests..."' in stale, stale[:400])
check("the banner says what to do with each answer, and not to guess",
      "Do NOT act on it yet" in stale and "Act only after the sender answers yes" in stale and "answers drop, discard it and do nothing" in stale
      and "if nobody answers, tell the user, do not guess" in stale)
check("the normal block and the normal ending follow the banner, unchanged", stale.endswith(plain) and "Never wait or poll" in stale)
long_text = "x" * 200
check("the question carries only the first 80 characters", ("x" * 80 + "...") in lib.render("a@b", long_text, waited_s=7200) and ("x" * 81) not in lib.render("a@b", long_text, waited_s=7200).split("RUNNING:")[1].split("  Act only")[0])
hostile = lib.render("a@b", 'rm -rf "$HOME" `id` \\ \nsecond line', waited_s=7200)
cmd_line = hostile.split("RUNNING: ")[1].split("  Act only")[0]
check("quotes, dollar signs, backticks, backslashes and newlines cannot break out of the quoted question",
      cmd_line.count('"') == 2 and "$" not in cmd_line and "`" not in cmd_line and "\\" not in cmd_line and "\n" not in cmd_line, cmd_line)
check("Grok's own wording also carries the banner and keeps its inbox ending",
      lib.GrokDoor().render("a@b", "x", waited_s=7200).startswith("STALE MAIL") and "post inbox" in lib.GrokDoor().render("a@b", "x", waited_s=7200)
      and "STALE MAIL" not in lib.GrokDoor().render("a@b", "x"))

door = Door([T1])
old = dict(HDR, **{"Sent-At": str(int(time.time()) - 7200)})
nc = handle(Msg("post.p.claude", "deploy it", old), door)
check("relay: mail sent 2 hours ago is delivered WITH the banner (and still acked and answered delivered)",
      door.delivered and door.delivered[0].startswith("STALE MAIL") and "waited 2 h 0 min" in door.delivered[0] and nc.sent[0][1]["status"] == "delivered")
door = Door([T1])
handle(Msg("post.p.claude", "deploy it", dict(HDR, **{"Sent-At": str(int(time.time()) - 60)})), door)
check("relay: mail sent a minute ago has no banner", door.delivered and "STALE MAIL" not in door.delivered[0])
door = Door([T1])
handle(Msg("post.p.claude", "deploy it", HDR, stored_ago=7200), door)
check("relay: no Sent-At (an older sender) falls back to how long the stream held it", door.delivered and door.delivered[0].startswith("STALE MAIL"))
door = Door([T1])
handle(Msg("post.p.claude", "deploy it", dict(HDR, **{"Sent-At": "garbage"}), stored_ago=5), door)
check("relay: a garbage Sent-At on fresh mail changes nothing (no banner, no crash)", door.delivered and "STALE MAIL" not in door.delivered[0])
door = Door([T1])
handle(Msg("post.p.claude", "deploy it", dict(HDR, **{"Sent-At": str(int(time.time()) - 29 * 60)})), door)
check("the limit is 30 minutes: mail sent 29 minutes ago has no banner", door.delivered and "STALE MAIL" not in door.delivered[0])
door = Door([T1])
handle(Msg("post.p.claude", "deploy it", dict(HDR, **{"Sent-At": str(int(time.time()) - 31 * 60)})), door)
check("the limit is 30 minutes: mail sent 31 minutes ago has the banner", door.delivered and door.delivered[0].startswith("STALE MAIL") and "waited 31 min" in door.delivered[0])
with mock.patch.object(lib, "STALE_AFTER_S", 10 ** 9):
    door = Door([T1])
    handle(Msg("post.p.claude", "deploy it", old), door)
check("the stale threshold is one setting (POSTBOX_STALE_AFTER_S)", door.delivered and "STALE MAIL" not in door.delivered[0])

# ---------------------------------------------------------------- 4. post.py: a Mac that is asleep gets its mail stored here
print("-- post: store for forwarding")


class JS:
    def __init__(self, nc): self.nc = nc
    async def publish(self, subject, data, headers=None, timeout=None):
        if self.nc.fail_publish:
            raise OSError("stream full")
        self.nc.published.append((subject, data, dict(headers or {})))


class LocalNC:
    """This Mac's own mailbox. `answer` is what the relay replies to a normal send (None: no reply)."""
    def __init__(self, answer=None, fail_publish=False):
        self.published, self.answer, self.fail_publish, self.closed = [], answer, fail_publish, False
    def new_inbox(self): return "_INBOX.t"
    async def subscribe(self, inbox): return self
    async def next_msg(self, timeout=None):
        from nats.errors import TimeoutError as NatsTimeout
        if self.answer is None:
            raise NatsTimeout()
        return types.SimpleNamespace(data=json.dumps(self.answer).encode())
    def jetstream(self): return JS(self)
    async def close(self): self.closed = True


def cli(argv, local=None, peer_error=None, peer_target_error=None, local_error=None, peer_nc=None):
    """Run `post <argv>` with fakes. `peer_error`: what opening the other Mac's mailbox raises. Returns (exit code, stdout, stderr)."""
    async def fake_connect(target, timeout=3):
        if target.label == "this Mac":
            if local_error:
                raise local_error
            return local
        if peer_error:
            raise peer_error
        return peer_nc

    def fake_peer_target(name):
        if peer_target_error:
            raise peer_target_error
        return lib.Target("nats://peer", None, name)

    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        for p in (mock.patch.object(lib, "connect_fast", fake_connect), mock.patch.object(lib, "peer_target", fake_peer_target),
                  mock.patch.object(lib, "local_target", lambda: lib.Target("nats://local", None, "this Mac")),
                  mock.patch.object(lib, "resolve_sender", lambda flag=None: (("claude", "My Proj", 4242), "test")),
                  contextlib.redirect_stdout(out), contextlib.redirect_stderr(err)):
            stack.enter_context(p)
        try:
            post.run(["post"] + argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return code, out.getvalue(), err.getvalue()


UNREACHABLE = lib.MailboxError("NoServersError()")
local = LocalNC()
before = int(time.time())
code, out, err = cli(["codex@project-b@mac-b", "please re-run the build"], local=local, peer_error=UNREACHABLE)
check("a sleeping Mac: exit 3 and the plain 'stored here, forwarded later' sentence",
      code == 3 and out.strip() == "queued: mac-b is not reachable right now; stored here and forwarded when it answers, kept for 7 days", (code, out, err))
subject, data, headers = local.published[0] if local.published else (None, None, {})
check("it is stored in THIS Mac's mailbox on forward.<machine>.<project>.<agent>, text unchanged", subject == "forward.mac-b.project-b.codex" and data == b"please re-run the build")
check("with the sender's full address (this Mac's name added), the original send time and a message id, and no reply inbox",
      headers.get("From") == "claude@my-proj@mac-a" and before <= int(headers.get("Sent-At", 0)) <= int(time.time()) + 1 and len(headers.get("Nats-Msg-Id", "")) == 32
      and "Reply-Inbox" not in headers and "From-Pid" not in headers, headers)
check("the connection is closed afterwards", local.closed)

for label, error, expected in (("this Mac's key refused by the other Mac", lib.MailboxError("Authorization Violation", refused=True), "refused this Mac's login key"),):
    local = LocalNC()
    code, out, err = cli(["codex@project-b@mac-b", "x"], local=local, peer_error=error)
    check(f"{label}: still dies with exit 2 and stores NOTHING", code == 2 and not local.published and expected in err and "queued" not in out, (code, err))
local = LocalNC()
code, out, err = cli(["codex@project-b@mac-b", "x"], local=local, peer_target_error=lib.MailboxError("no other Mac called 'mac-b' is linked to this one (linked: none)", setup=True))
check("a Mac that is not linked (setup problem): dies with exit 2 and stores NOTHING", code == 2 and not local.published and "no other Mac called" in err, (code, err))
local = LocalNC()
code, out, err = cli(["codex@project-b@mac-b", "x"], local=local, peer_target_error=lib.MailboxError("cannot find mac-b on the network", setup=True, transient=True))
check("a linked Mac whose name does not resolve right now (Tailscale off): stored for forwarding, exit 3, like an asleep Mac",
      code == 3 and len(local.published) == 1 and local.published[0][0].startswith("forward.mac-b.") and "stored here and forwarded" in out, (code, out, err, local.published))
local = LocalNC()
code, out, err = cli(["codex@project-b@mac-b", "x"], local=local, peer_target_error=lib.MailboxError("this Mac has no login key yet, so it cannot use mac-b's post box", setup=True))
check("a setup problem that needs a person (no login key yet): still dies with exit 2 and stores NOTHING", code == 2 and not local.published and "no login key" in err, (code, err))
# the REAL peer_target: an unresolvable host name must come out transient, an unknown peer or a missing key must not
import os
from unittest import mock as _mock
def real_peer_target_error(link, seed, env=None):
    with _mock.patch.object(lib, "load_link", return_value=link), _mock.patch.object(lib, "machine_seed", return_value=seed), \
         _mock.patch.object(lib, "_ipv4", side_effect=OSError("[Errno 8] nodename nor servname provided, or not known")), \
         _mock.patch.dict(os.environ, env or {}):
        try:
            lib.peer_target("mac-b")
        except lib.MailboxError as e:
            return e
    return None
e = real_peer_target_error({"peers": {"mac-b": {"host": "mac-b-x.ts.net"}}}, "SEED")
check("peer_target: a host name that does not resolve is a TRANSIENT setup error", e is not None and e.setup and e.transient and "cannot find mac-b" in str(e), e)
e = real_peer_target_error({"peers": {}}, "SEED")
check("peer_target: an unknown peer is a setup error that is NOT transient (needs a person)", e is not None and e.setup and not e.transient, e)
e = real_peer_target_error({"peers": {"mac-b": {"host": "mac-b-x.ts.net"}}}, None)
check("peer_target: a missing login key is a setup error that is NOT transient", e is not None and e.setup and not e.transient, e)
check("link_outcome: transient setup counts as unreachable, plain setup as setup, refused as refused",
      [post.link_outcome(x) for x in (lib.MailboxError("a", setup=True, transient=True), lib.MailboxError("b", setup=True), lib.MailboxError("c", refused=True), lib.MailboxError("d"))]
      == ["unreachable", "setup", "refused", "unreachable"])
local = LocalNC(fail_publish=True)
code, out, err = cli(["codex@project-b@mac-b", "x"], local=local, peer_error=UNREACHABLE)
check("the other Mac is asleep AND the local store fails: exit 2, it says nothing was stored (never a false 'queued')",
      code == 2 and "queued" not in out and "could NOT be stored" in err and "nothing was sent" in err, (code, out, err))
code, out, err = cli(["codex@project-b@mac-b", "x"], local_error=lib.MailboxError("NoServersError()"), peer_error=UNREACHABLE)
check("the other Mac is asleep AND this Mac's own mailbox is down: exit 2, nothing stored", code == 2 and "queued" not in out and "could NOT be stored" in err, (code, out, err))

# Sent-At on EVERY send
local = LocalNC({"status": "delivered", "detail": "delivered to claude@p (pid 5)", "targets": ["claude@p (pid 5)"]})
code, out, err = cli(["claude@p", "hello"], local=local)
h = local.published[0][2]
check("a local send carries Sent-At too (and still the reply inbox and the sender's process number)",
      code == 0 and local.published[0][0] == "post.p.claude" and h.get("Sent-At", "").isdigit() and h.get("Reply-Inbox") == "_INBOX.t" and h.get("From-Pid") == "4242" and h["From"] == "claude@my-proj", h)
peer = LocalNC({"status": "delivered", "detail": "delivered to codex@project-b (pid 9)", "targets": ["codex@project-b (pid 9)"]})
code, out, err = cli(["codex@project-b@mac-b", "hello"], local=LocalNC(), peer_nc=peer)
h = peer.published[0][2]
check("a send to a Mac that IS awake goes straight there (nothing stored here) and carries Sent-At and this Mac's name",
      code == 0 and peer.published[0][0] == "post.project-b.codex" and h.get("Sent-At", "").isdigit() and h["From"] == "claude@my-proj@mac-a" and "From-Pid" not in h, (code, out, err))
check("headers still hold the sender-name cleaning (a folder called 'My Proj' becomes my-proj)", h["From"].startswith("claude@my-proj"))


# ---------------------------------------------------------------- 5. the forwarder
print("-- forwarder")


class PeerNC:
    """The other Mac's mailbox as the forwarder sees it. `answer`: what its relay replies (None: no reply in time)."""
    def __init__(self, answer=None, fail_publish=False):
        self.answer, self.fail_publish, self.published, self.closed = answer, fail_publish, [], False
    def new_inbox(self): return "_INBOX.pabc.1"
    async def subscribe(self, inbox): return self
    async def next_msg(self, timeout=None):
        if self.answer is None:
            raise asyncio.TimeoutError()
        return types.SimpleNamespace(data=json.dumps(self.answer).encode())
    def jetstream(self): return JS(self)
    async def close(self): self.closed = True


def forward(msg, peer=None, connect_error=None, target_error=None):
    calls = []

    async def connect(target, timeout=3):
        calls.append(target.label)
        if connect_error:
            raise connect_error
        return peer

    def target_of(name):
        if target_error:
            raise target_error
        return lib.Target("nats://peer", None, name)

    with contextlib.redirect_stdout(io.StringIO()):
        run(relay.handle_forward(msg, connect=connect, peer_target=target_of))
    return calls


def fresh_state():
    relay.PEER_DOWN.clear()
    relay.FWD_WAITING.clear()
    relay.STATS.reset()


FHDR = {"From": "claude@my-proj@mac-a", "Nats-Msg-Id": "a" * 32, "Sent-At": str(int(time.time()) - 5000)}

fresh_state()
peer = PeerNC({"status": "delivered", "detail": "delivered to codex@project-b (pid 9)"})
m = Msg("forward.mac-b.project-b.codex", "re-run it", FHDR)
forward(m, peer)
subject, data, headers = peer.published[0] if peer.published else (None, None, {})
check("the other Mac answered delivered: the stored mail is acked (nak never used)", m.acked and not m.naked and not m.termed)
check("it was published to the other Mac as post.<project>.<agent>, text unchanged",
      subject == "post.project-b.codex" and data == b"re-run it")
check("with the original From, original Sent-At, original message id and a reply inbox of the allowed name",
      headers["From"] == "claude@my-proj@mac-a" and headers["Sent-At"] == FHDR["Sent-At"] and headers["Nats-Msg-Id"] == "a" * 32 and headers["Reply-Inbox"] == "_INBOX.pabc.1", headers)
check("the connection to the other Mac is closed, and the forward is counted", peer.closed and relay.STATS.forwards["forwarded"] == 1)

for answer, label in (({"status": "queued", "detail": "nobody reachable", "reason": "nobody_there"}, "queued"), ({"status": "refused", "detail": "no delivery door", "reason": "no_door"}, "refused (no door)"),
                      (None, "no answer in time")):
    fresh_state()
    m = Msg("forward.mac-b.project-b.codex", "x", FHDR)
    forward(m, PeerNC(answer))
    check(f"the other Mac answered {label}: acked anyway (it owns the mail now)", m.acked and not m.naked)

fresh_state()
m = Msg("forward.mac-b.project-b.codex", "x", FHDR)
forward(m, PeerNC({"status": "refused", "detail": "stop rule: more than 30 messages", "reason": "pair_limit"}))
check("the other Mac refused for a stop rule: the mail is kept and tried again, not lost", m.naked == relay.FORWARD_RETRY_S and not m.acked and relay.STATS.forwards["stored"] == 1)

fresh_state()
m = Msg("forward.mac-b.project-b.codex", "x", FHDR, seq=41)
calls = forward(m, connect_error=UNREACHABLE)
check("the other Mac is asleep: nak with a 60 s delay, not acked, not dropped; remembered as waiting",
      m.naked == 60 and not m.acked and not m.termed and calls == ["mac-b"] and 41 in relay.FWD_WAITING and relay.STATS.forwards["stored"] == 1)
m2 = Msg("forward.mac-b.project-b.codex", "y", FHDR, seq=42)
calls = forward(m2, connect_error=UNREACHABLE)
check("a second waiting message does not try the sleeping Mac again at once (one try per Mac per round)", m2.naked == 60 and calls == [] and 42 in relay.FWD_WAITING)
relay.PEER_DOWN.clear()
m3 = Msg("forward.mac-b.project-b.codex", "x", FHDR, delivered=4, seq=41)
forward(m3, connect_error=UNREACHABLE)
check("later retries only count (no new 'stored' event each minute)", m3.naked == 60 and relay.STATS.forwards["stored"] == 2 and relay.STATS.retry_naks == 1)
relay.PEER_DOWN.clear()
m4 = Msg("forward.mac-b.project-b.codex", "x", FHDR, delivered=5, seq=41)
forward(m4, PeerNC({"status": "delivered", "detail": "d"}))
check("when the Mac wakes up, the waiting mail is forwarded, acked and leaves the waiting list", m4.acked and 41 not in relay.FWD_WAITING)

fresh_state()
m = Msg("forward.mac-b.project-b.codex", "x", FHDR)
forward(m, connect_error=lib.MailboxError("Authorization Violation", refused=True))
check("the other Mac rejects this Mac's key: kept and retried (a person fixes the link), never silently dropped", m.naked == 60 and not m.acked and not m.termed)
fresh_state()
m = Msg("forward.mac-b.project-b.codex", "x", FHDR)
forward(m, PeerNC(fail_publish=True))
check("the connection breaks before the other Mac stored it: kept and retried", m.naked == 60 and not m.acked)
fresh_state()
m = Msg("forward.nowhere.project-b.codex", "x", FHDR, seq=50)
calls = forward(m, target_error=lib.MailboxError("no other Mac called 'nowhere' is linked to this one (linked: mac-b)", setup=True))
check("a Mac that is not in link.json: the mail is refused for good (term), not retried for 7 days, and the connection is never attempted",
      m.termed and not m.naked and not m.acked and calls == [] and relay.STATS.forwards["refused"] == 1)
fresh_state()
m = Msg("forward.mac-b.project-b.codex", "x", FHDR)
forward(m, target_error=lib.MailboxError("cannot find mac-b on the network (host: nodename nor servname provided)", setup=True))
check("a Mac whose name does not resolve right now: kept and retried (Tailscale may just be off)", m.naked == 60 and not m.termed)
fresh_state()
m = Msg("forward.garbage", "x", FHDR)
forward(m, PeerNC({"status": "delivered", "detail": "d"}))
check("a malformed forward subject is dropped", m.termed and not m.acked)
fresh_state()
bad = Msg("forward.mac-b.project-b.codex", "x", {"From": "claude@ok", "Sent-At": "garbage; rm"})
peer = PeerNC({"status": "delivered", "detail": "d"})
forward(bad, peer)
check("a garbage Sent-At is not passed on, and a missing message id gets a fresh one", "Sent-At" not in peer.published[0][2] and len(peer.published[0][2]["Nats-Msg-Id"]) == 32 and bad.acked)

# expiry of forward mail
fresh_state()
relay.FWD_WAITING[1] = {"stored": time.time() - lib.MAX_AGE_S - 600, "frm": "claude@a@mac-a", "to": "codex@b", "machine": "mac-b"}
relay.FWD_WAITING[2] = {"stored": time.time() - 30, "frm": "claude@a@mac-a", "to": "codex@b", "machine": "mac-b"}
with contextlib.redirect_stdout(io.StringIO()):
    n = relay.sweep_expired_forwards()
check("forward mail that outlived the stream's age limit is counted once as expired; fresh mail is left alone",
      n == 1 and list(relay.FWD_WAITING) == [2] and relay.STATS.forwards["expired"] == 1 and relay.sweep_expired_forwards() == 0)
check("the hourly summary carries the forward counts", relay.STATS.summary()["forwards"] == {"expired": 1})


# ---------------------------------------------------------------- 6. the stream is brought up to date
print("-- ensure_stream")


class StreamJS:
    def __init__(self, max_age, subjects, size=64000, fail=False):
        self.config = types.SimpleNamespace(max_age=max_age, subjects=list(subjects), max_msg_size=size)
        self.updates, self.fail = [], fail

    async def stream_info(self, name): return types.SimpleNamespace(config=self.config)

    async def update_stream(self, cfg):
        if self.fail:
            raise RuntimeError("server said no")
        self.updates.append((cfg.max_age, list(cfg.subjects)))


def ensure(js):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(relay.ensure_stream(js))
    return [json.loads(line)["event"] for line in buf.getvalue().splitlines() if line.strip().startswith("{")]


js = StreamJS(3600.0, ["post.>"])
events = ensure(js)
check("a stream made by the old version gets the 7-day limit and forward.> (one update, both logged)",
      js.updates and js.updates[-1] == (lib.MAX_AGE_S, ["post.>", "forward.>"]) and "stream_max_age_updated" in events and "stream_subjects_updated" in events, (js.updates, events))
js = StreamJS(float(lib.MAX_AGE_S), ["post.>", "forward.>"])
check("a stream that is already current is left alone (no update, nothing logged)", ensure(js) == [] and js.updates == [])
js = StreamJS(3600.0 * 1e9, ["post.>", "forward.>"])
events = ensure(js)
check("an age limit reported in nanoseconds is compared correctly (3600 s is old, 7 days in ns is current)",
      "stream_max_age_updated" in events and ensure(StreamJS(lib.MAX_AGE_S * 1e9, ["post.>", "forward.>"])) == [])
js = StreamJS(float(lib.MAX_AGE_S), ["post.>"])
events = ensure(js)
check("only the subject is missing: only the subject is fixed", events == ["stream_subjects_updated"] and js.updates[-1][1] == ["post.>", "forward.>"])
js = StreamJS(3600.0, ["post.>"], fail=True)
events = ensure(js)
check("a refused update never fails the relay: each failure is logged as relay_error and the function returns", events.count("relay_error") == 2, events)
js = StreamJS(3600.0, ["post.>"], size=0)
check("the old message-size fix still works next to the new ones", "stream_size_cap_added" in ensure(js) and js.config.max_msg_size == relay.MAX_MSG_BYTES)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
