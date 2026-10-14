"""Hardening tests for the cross-machine feature. Each block reproduces a known defect first:

  - a linked Mac's key could listen on _INBOX.> and read this Mac's mail (the relay's deliveries and replies travel on _INBOX names)
  - a hostile Reply-Inbox or From-Pid stalled the relay and leaked a connection; the relay could be made to publish to any subject
  - a sender folder called "My Project" became a reply command the shell splits into the wrong address
  - oversize mail from a linked Mac was stored and delivered; a sender could dodge the stop rule by changing its name
  - the stop rule counted redeliveries, so queued mail was dropped after about 5 minutes instead of being kept for an hour
  - a damaged key file read as "the post box is not running"; a relay with a refused login logged a traceback several times a second

Real nats-server and relay processes are used where the defect lives in the wire rules. Run: .venv/bin/python tests/test_hardening.py
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool (tests/test_telemetry.py covers telemetry itself)
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
sys.path.insert(0, str(ROOT / "postbox"))
os.environ["POSTBOX_MACHINE"] = "alpha"          # no check may depend on this Mac's real name
import install  # noqa: E402
import lib  # noqa: E402
import nkeys  # noqa: E402
import post as post_cli  # noqa: E402
import relay  # noqa: E402

PY = sys.executable
RELAY = str(ROOT / "postbox" / "relay.py")
NATS_BIN = install.find_nats()
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(fn, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


def keypair():
    seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER)
    return seed.decode(), nkeys.from_seed(seed).public_key.decode()


# ------------------------------------------------------------------ pure rules
print("address and inbox rules")
for good in ("_INBOX.abc", "_INBOX.pKIO6SXNJ5GEELD6Y.nuid.token", "_INBOX.a-b_c.d"):
    check(f"a real reply inbox is accepted: {good}", bool(lib.INBOX_RE.match(good)))
for bad in ("", "a b", "$JS.API.STREAM.PURGE.POST", "_INBOX", "_INBOX.", "_INBOX.a b", "_INBOX.x\r\ny", "_INBOX.a\tb", "post.x.y", "_INBOX.a.b.c.d.e", "_INBOX.$JS"):
    check(f"a hostile reply inbox is refused: {bad!r}", not lib.INBOX_RE.match(bad))
check("sender address: spaces and capitals become the address form", lib.clean_address("claude@My Project@alpha") == "claude@my-project@alpha")
check("sender address: shell metacharacters cannot survive", lib.clean_address("claude@$(reboot);x") == "claude@reboot-x" and "$" not in lib.clean_address("a@$b"))
check("sender address: too many parts or an empty part is unknown", lib.clean_address("a@b@c@d") == "unknown" and lib.clean_address("a@@b") == "unknown" and lib.clean_address("") == "unknown" and lib.clean_address(None) == "unknown")
ka, kb = keypair()[1], keypair()[1]
check("a peer's inbox name comes from its public key: stable, different per key, never the default name",
      lib.peer_inbox_prefix(ka) == lib.peer_inbox_prefix(ka) != lib.peer_inbox_prefix(kb) and lib.peer_inbox_prefix(ka).startswith("_INBOX.p") and lib.peer_inbox_prefix(ka).count(".") == 1)

# ------------------------------------------------------------------ relay decisions with fakes
print("relay decisions (fakes)")


class FakeMsg:
    def __init__(self, subject, data, headers, delivered=1):
        self.subject, self.data, self.headers = subject, data if isinstance(data, bytes) else data.encode(), headers
        self.metadata = types.SimpleNamespace(num_delivered=delivered)
        self.acked = self.naked = self.termed = None

    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): self.termed = True


class FakeNC:
    def __init__(self, fail_publish=False): self.sent, self.fail = [], fail_publish

    async def publish(self, subj, data):
        if self.fail:
            raise OSError("connection lost")
        self.sent.append((subj, json.loads(data)))

    async def flush(self, timeout=None): pass


class FakeDoor:
    def __init__(self, targets): self.targets, self.delivered, self.pids = targets, [], []

    def resolve(self, project, exclude_pid=None):
        self.pids.append(exclude_pid)
        return self.targets

    def deliver(self, t, text): self.delivered.append(text)


SESSION = lib.Session("claude", 5, "/p/a", "a", "sock")
run = asyncio.run


def handle(headers, *, data="hello", delivered=1, door=None, nc=None, limiter=None, recipient_limiter=None):
    m, nc, door = FakeMsg("post.a.claude", data, headers, delivered), nc or FakeNC(), door or FakeDoor([SESSION])
    run(relay.handle(nc, m, {"claude": door}, limiter or lib.PairLimiter(), recipient_limiter or lib.PairLimiter(limit=1000)))
    return m, nc, door


for bad in ("a b", "$JS.API.STREAM.PURGE.POST", "_INBOX.x\r\nPUB evil 1", "_INBOX.a\tb", "post.x.y", ""):
    m, nc, door = handle({"From": "claude@b", "Reply-Inbox": bad})
    check(f"hostile Reply-Inbox {bad[:26]!r}: mail still delivered and acknowledged, nothing published to that name", m.acked and door.delivered and nc.sent == [])
m, nc, door = handle({"From": "claude@b", "Reply-Inbox": "_INBOX.abc.def"})
check("a real Reply-Inbox is still answered", nc.sent and nc.sent[0][0] == "_INBOX.abc.def" and nc.sent[0][1]["status"] == "delivered")
m, nc, door = handle({"From": "claude@b", "Reply-Inbox": "_INBOX.abc"}, nc=FakeNC(fail_publish=True))
check("a reply that cannot be sent never stops the mail from being acknowledged", m.acked and door.delivered)
for pid, want in (("42", 42), ("²", None), ("١٢٣", None), ("-5", None), ("12345678901", None), ("", None)):
    m, nc, door = handle({"From": "claude@b", "From-Pid": pid})
    check(f"From-Pid {pid!r} is read as {want}", door.pids == [want])
m, nc, door = handle({"From": "claude@My Project@alpha", "Reply-Inbox": "_INBOX.a"})
body = door.delivered[0]
check("a sender folder called 'My Project' gives a reply command that is exactly one address", 'post claude@my-project@alpha "your reply"' in body and shlex.split('post claude@my-project@alpha "your reply"')[1] == "claude@my-project@alpha")
m, nc, door = handle({"From": "a@b@c@d"})
check("a sender that cannot be made into an address is unknown, not passed through", "post unknown " in door.delivered[0])
m, nc, door = handle({"From": "claude@b"}, data="x" * (lib.MAX_TEXT_BYTES + 1), nc=FakeNC())
check("oversize mail is refused by the relay itself, not delivered", m.termed and door.delivered == [])
m, nc, door = handle({"From": "claude@b", "Reply-Inbox": "_INBOX.a"}, data="x" * (lib.MAX_TEXT_BYTES + 1))
check("...and the sender is told why in plain words", nc.sent and nc.sent[0][1]["status"] == "refused" and "too long" in nc.sent[0][1]["detail"])
rl = lib.PairLimiter(limit=2, window=60)
results = [handle({"From": f"claude@sender{i}", "Reply-Inbox": "_INBOX.a"}, recipient_limiter=rl)[1].sent[0][1]["status"] for i in range(3)]
check("a sender cannot dodge the stop rule by changing its name: all senders together are limited per recipient", results == ["delivered", "delivered", "refused"], str(results))
m, nc, door = handle({"From": "claude@b", "Reply-Inbox": "_INBOX.a"}, delivered=7, door=FakeDoor([]), limiter=lib.PairLimiter(limit=0))
check("redeliveries of queued mail are not counted against the stop rule (they used to drop mail after about 5 minutes)", m.naked == 10 and not m.termed)

# ------------------------------------------------------------------ post: the sender address it sends
print("post sender address")


class CaptureNC:
    def __init__(self): self.headers = None
    def new_inbox(self): return "_INBOX.cap"
    async def subscribe(self, subject):
        class Sub:
            async def next_msg(self, timeout=None): return types.SimpleNamespace(data=json.dumps({"status": "delivered", "detail": "x"}).encode())
        return Sub()
    def jetstream(self):
        outer = self

        class JS:
            async def publish(self, subject, payload, headers=None, timeout=None): outer.headers = headers
        return JS()
    async def close(self): pass


cap = CaptureNC()


async def fake_connect(target, timeout=3):
    return cap


real_connect = lib.connect_fast
lib.connect_fast = fake_connect
try:
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            run(post_cli.send("claude", "proj", None, "hi", ("claude", "My Project", 123)))
        except SystemExit:
            pass
finally:
    lib.connect_fast = real_connect
check("post sends the project in its address form, never the raw folder name", cap.headers and cap.headers["From"] == "claude@my-project" and cap.headers["From-Pid"] == "123", str(cap.headers))

# ------------------------------------------------------------------ a damaged key file
print("a damaged key file")
with tempfile.TemporaryDirectory() as td:
    os.environ["POSTBOX_SUPPORT_DIR"] = td
    (Path(td) / "machine.seed").write_text("this is not a key\n")
    try:
        lib.local_target()
        check("a key file that is not a key is an error in plain words", False, "(no error)")
    except lib.MailboxError as exc:
        check("a key file that is not a key is an error in plain words, and never echoes its contents", exc.setup and "damaged" in str(exc) and "this is not a key" not in str(exc), str(exc))
    err = io.StringIO()
    code = None
    with contextlib.redirect_stderr(err):
        try:
            run(post_cli.send("claude", "proj", None, "hi", ("claude", "proj", 1)))
        except SystemExit as e:
            code = e.code
    check("post says the key file is damaged instead of claiming the post box is not running", code == 2 and "damaged" in err.getvalue() and "not running" not in err.getvalue(), err.getvalue()[:160])
    del os.environ["POSTBOX_SUPPORT_DIR"]

if not NATS_BIN:
    print("skip the wire tests: nats-server is not installed")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

import nats  # noqa: E402
from nats.js.api import RetentionPolicy, StorageType, StreamConfig  # noqa: E402

procs = []
TMP = tempfile.mkdtemp(prefix="pbh", dir="/tmp")   # a short path: a Unix socket path is limited to about 100 characters


def start_server(local_pub, peers):
    port = free_port()
    store = Path(TMP) / f"store{port}"
    (Path(TMP) / f"n{port}.conf").write_text(install.nats_conf("hard", port, store, local_pub, peers))
    proc = subprocess.Popen([NATS_BIN, "-c", str(Path(TMP) / f"n{port}.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(proc)
    assert wait_for(lambda: install.port_in_use(port)), "the test mailbox did not start"
    return port


FAKE = r'''
import os, socket, sys
sockdir, out = sys.argv[2], sys.argv[3]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(os.path.join(sockdir, str(os.getpid()) + ".sock")); s.listen(5)
print("ready", flush=True)
while True:
    c, _ = s.accept()
    data = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            break
        data += chunk
    c.close()
    open(out, "ab").write(data)
'''

try:
    # ---------------------------------------------------------------- a linked key cannot read local mail
    print("a linked Mac's key against a real mailbox")
    lseed, lpub = keypair()
    pseed, ppub = keypair()
    oseed, opub = keypair()
    port = start_server(lpub, [("mac-b", ppub), ("other", opub)])
    url = f"nats://127.0.0.1:{port}"

    async def wire_permissions():
        out = {}
        errors = []

        async def on_error(e=None):
            errors.append(repr(e))

        async def until(pred, secs=3.0):
            """Wait for the event we expect instead of sleeping a fixed time: a busy machine must not turn a correct result into a failure."""
            end = time.time() + secs
            while time.time() < end and not pred():
                await asyncio.sleep(0.05)

        local = await nats.connect(url, nkeys_seed_str=lseed, error_cb=on_error)
        js = local.jetstream()
        await js.add_stream(StreamConfig(name="POST", subjects=["post.>"], retention=RetentionPolicy.WORK_QUEUE, storage=StorageType.FILE))
        pull = await js.pull_subscribe("post.>", durable="relay", stream="POST")

        # a peer that still uses the DEFAULT inbox name (an old client) tries to listen on everything under _INBOX
        old = await nats.connect(url, nkeys_seed_str=pseed, error_cb=on_error)
        heard = []

        async def on_msg(m):
            heard.append(m.data)

        errors.clear()
        await old.subscribe("_INBOX.>", cb=on_msg)
        await old.flush(timeout=2)
        await until(lambda: any("ermission" in e for e in errors))
        out["wide_denied"] = any("ermission" in e for e in errors)
        # the exact exploit from the review: a local mail is delivered to the relay over a pull consumer, which uses _INBOX names
        await js.publish("post.x.y", b"SECRET-LOCAL-MAIL-BODY")
        got = await pull.fetch(1, timeout=3)
        await got[0].ack()
        await asyncio.sleep(0.4)
        out["leaked"] = any(b"SECRET-LOCAL-MAIL-BODY" in d for d in heard)
        errors.clear()
        await old.subscribe(lib.peer_inbox_prefix(opub) + ".>", cb=on_msg)   # another linked Mac's inbox name
        await old.flush(timeout=2)
        await until(lambda: any("ermission" in e for e in errors))
        out["other_denied"] = any("ermission" in e for e in errors)
        await old.close()

        # the same key with ITS OWN inbox name: what post really uses
        mine = lib.peer_inbox_prefix(ppub)
        new = await nats.connect(url, nkeys_seed_str=pseed, inbox_prefix=mine, error_cb=on_error)
        replies = []

        async def on_reply(m):
            replies.append(m.data)

        async def answer(m):
            await local.publish(m.reply, b'{"machine":"alpha"}')

        await local.subscribe("who.sessions", cb=answer)
        await local.flush()
        errors.clear()
        await new.subscribe(mine + ".direct", cb=on_reply)
        await new.flush(timeout=2)
        await local.publish(mine + ".direct", b"a reply for this peer")
        await local.flush()
        await until(lambda: bool(replies))
        out["own_inbox_works"] = replies == [b"a reply for this peer"] and not errors
        out["own_detail"] = f"replies={replies} errors={errors}"
        try:
            who = await new.request("who.sessions", b"", timeout=2)
            out["who_works"] = b"alpha" in who.data
        except Exception:  # noqa: BLE001
            out["who_works"] = False
        ack = await new.jetstream().publish("post.x.y", b"hello from the peer", timeout=3)
        out["publish_ack_works"] = ack.seq >= 1
        await new.close()
        await local.close()
        return out

    w = asyncio.run(wire_permissions())
    check("a linked key is refused a listen on _INBOX.> (what let it read local mail)", w["wide_denied"])
    check("the review's exploit fails: a local mail delivered over a pull consumer is NOT heard by the linked key", not w["leaked"])
    check("a linked key cannot listen on another linked Mac's inbox name either", w["other_denied"])
    check("the same key still hears replies on its OWN inbox name", w["own_inbox_works"], w["own_detail"])
    check("...and who requests and publishing with the receipt still work through that name", w["who_works"] and w["publish_ack_works"])

    # ---------------------------------------------------------------- the relay against hostile headers, through a linked key
    print("the relay against hostile mail from a linked key")
    socks = Path(TMP) / "socks"
    socks.mkdir()
    support = Path(TMP) / "support"
    support.mkdir()
    (support / "machine.seed").write_text(lseed + "\n")
    (support / "link.json").write_text(json.dumps({"machine": "alpha", "peers": {}}))
    folder = Path(TMP) / "proj-z"
    folder.mkdir()
    frames = Path(TMP) / "proj-z.frames"
    fake = subprocess.Popen([PY, "-c", FAKE, "claude-fake-session", str(socks), str(frames)], cwd=folder, stdout=subprocess.PIPE, text=True)
    procs.append(fake)
    fake.stdout.readline()
    env = dict(os.environ, POSTBOX_NATS_URL=url, POSTBOX_SUPPORT_DIR=str(support), POSTBOX_CLAUDE_SOCK_DIR=str(socks))
    env.pop("POSTBOX_MACHINE", None)
    log = Path(TMP) / "relay.log"
    rel = subprocess.Popen([PY, RELAY], env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
    procs.append(rel)
    assert wait_for(lambda: "relay_ready" in log.read_text()), log.read_text()[-300:]

    async def hostile():
        mine = lib.peer_inbox_prefix(ppub)
        peer = await nats.connect(url, nkeys_seed_str=pseed, inbox_prefix=mine)
        js = peer.jetstream()
        for i, hdr in enumerate(({"Reply-Inbox": "a b", "From-Pid": "²"}, {"Reply-Inbox": "a\tb\r\nPUB", "From-Pid": "x"},
                                 {"Reply-Inbox": "$JS.API.STREAM.PURGE.POST", "From": "claude@My Project@mac-b"})):
            headers = {"From": "claude@evil@mac-b", "Nats-Msg-Id": f"hostile{i}"}
            headers.update(hdr)
            await js.publish("post.proj-z.claude", f"hostile mail {i}".encode(), headers=headers, timeout=3)
        await asyncio.sleep(0.2)
        inbox = peer.new_inbox()
        seen = []

        async def collect(m):
            seen.append(m.data)

        await peer.subscribe(inbox, cb=collect)
        await peer.publish("who.sessions", b"", reply=inbox)
        await peer.flush(timeout=2)
        await asyncio.sleep(1.5)
        await peer.close()
        return len(seen)

    t0 = time.time()
    who_replies = asyncio.run(hostile())
    delivered = wait_for(lambda: frames.exists() and frames.read_text(errors="replace").count("hostile mail") == 3, 12)
    relay_log = log.read_text()
    check("three hostile mails are all delivered (no stall, no retry loop)", delivered and time.time() - t0 < 20, frames.read_text(errors="replace")[-200:] if frames.exists() else "no frames")
    check("the relay logged each bad reply name and raised no error", relay_log.count("bad_reply_inbox") >= 2 and "handle_error" not in relay_log and "relay_error" not in relay_log, relay_log[-400:])
    check("one who request is answered exactly once (no leaked connections answering twice)", who_replies == 1, f"replies={who_replies}")
    body = frames.read_text(errors="replace")
    check("a hostile sender name reaches the receiver only in address form", "post claude@my-project@mac-b" in body.replace('\\"', '"') or "post claude@evil@mac-b" in body.replace('\\"', '"'))

    # ---------------------------------------------------------------- oversize and the stream's own cap
    print("message size")

    async def size_cases():
        local = await nats.connect(url, nkeys_seed_str=lseed)
        js = local.jetstream()
        info = await js.stream_info("POST")
        cap = info.config.max_msg_size
        try:
            await js.publish("post.x.y", b"x" * 100_000, timeout=3)
            refused = "accepted"
        except Exception as exc:  # noqa: BLE001
            refused = f"{type(exc).__name__}: {exc}"
        await local.close()
        return cap, refused

    cap, refused = asyncio.run(size_cases())
    check("a 100 KB mail is refused by the stream itself, for that reason (a timeout or a dropped connection would not count)",
          refused.startswith("BadRequestError") and "err_code=10054" in refused and "message size exceeds maximum allowed" in refused, f"cap={cap} {refused}")

    # a brand new mailbox whose stream the RELAY itself creates (the test above made its own stream first, so only the update path ran there)
    port3 = start_server(lpub, [])
    env3 = dict(env, POSTBOX_NATS_URL=f"nats://127.0.0.1:{port3}")
    log3 = Path(TMP) / "relay3.log"
    rel3 = subprocess.Popen([PY, RELAY], env=env3, stdout=open(log3, "w"), stderr=subprocess.STDOUT)
    procs.append(rel3)
    assert wait_for(lambda: "relay_ready" in log3.read_text()), log3.read_text()[-300:]

    async def created_cap():
        local = await nats.connect(f"nats://127.0.0.1:{port3}", nkeys_seed_str=lseed)
        cap3 = (await local.jetstream().stream_info("POST")).config.max_msg_size
        await local.close()
        return cap3

    check("a stream CREATED by the relay carries the message size cap from the start", "stream_created" in log3.read_text() and asyncio.run(created_cap()) == relay.MAX_MSG_BYTES)

    async def old_stream_gets_cap():
        port2 = start_server(lpub, [])
        local = await nats.connect(f"nats://127.0.0.1:{port2}", nkeys_seed_str=lseed)
        js = local.jetstream()
        await js.add_stream(StreamConfig(name="POST", subjects=["post.>"], retention=RetentionPolicy.WORK_QUEUE, storage=StorageType.FILE))
        before = (await js.stream_info("POST")).config.max_msg_size
        with contextlib.redirect_stdout(io.StringIO()):
            await relay.ensure_stream(js)
        after = (await js.stream_info("POST")).config.max_msg_size
        await local.close()
        return before, after

    before, after = asyncio.run(old_stream_gets_cap())
    check("a stream made before the cap existed gets the cap when the relay starts", before in (None, -1, 0) and after == relay.MAX_MSG_BYTES, f"{before} -> {after}")

    # ---------------------------------------------------------------- a relay whose login is refused
    print("a relay whose login is refused")
    wseed, _ = keypair()
    bad_support = Path(TMP) / "bad_support"
    bad_support.mkdir()
    (bad_support / "machine.seed").write_text(wseed + "\n")
    bad_env = dict(env, POSTBOX_SUPPORT_DIR=str(bad_support))
    bad_log = Path(TMP) / "bad_relay.log"
    bad = subprocess.Popen([PY, RELAY], env=bad_env, stdout=open(bad_log, "w"), stderr=subprocess.STDOUT)
    procs.append(bad)
    time.sleep(8)
    text = bad_log.read_text()
    check("it says in the log that the login is refused (the health check reads relay_error lines)", '"event": "relay_error"' in text and "uthorization" in text, text[-300:])
    check("...once a minute, not several tracebacks a second", len(text) < 2500 and text.count("Traceback") == 0, f"{len(text)} bytes, {text.count('Traceback')} tracebacks")
finally:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
