#!/usr/bin/env python3
"""Offline tests for the wake webhook (the doorbell for inbox-only agents such as grokbot-desktop): a delivery into its inbox calls
its webhook once, a failing webhook never changes the delivery, the secrets are only ever 1Password references resolved through an injected
resolver (op-cached is never run), the call is IPv4 only, the sender's answer says what happened, and `post wake-test` rings it once.
The "webhook" is a tiny HTTP server on this Mac. No real mailbox, no real app, no 1Password, nothing leaves this process.
Run: .venv/bin/python tests/test_wake_webhook.py"""
import asyncio
import contextlib
import http.server
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool
import socket
import sys
import tempfile
import threading
import time
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib  # noqa: E402
import post as post_cli  # noqa: E402
import relay  # noqa: E402
import wake  # noqa: E402

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
REAL_WAKER = wake.Waker
URL_REF, KEY_REF = "op://<vault>/<item>/url", "op://<vault>/<item>/key"
SECRET_KEY = "k-secret-0123456789"


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


class Hook:
    """The fake webhook: records every request, answers `status` after `delay` seconds."""

    def __init__(self, status=200, delay=0.0):
        outer = self
        self.calls, self.status, self.delay = [], status, delay

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.calls.append({"path": self.path, "headers": dict(self.headers), "json": json.loads(body)})
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b"ok")
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)   # IPv4 only, on purpose
        self.port = self.server.server_address[1]
        self.url = f"http://localhost:{self.port}/hook/abc123"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


HDR = {"From": "claude@claude", "From-Pid": "11", "Reply-Inbox": "_INBOX.x"}


def make_waker(url, resolver_calls=None, fail=False, timeout_s=5, clock=time.time, agent=GBD, desks=None):
    refs = resolver_calls if resolver_calls is not None else []

    def resolver(ref):
        refs.append(ref)
        if fail:
            raise RuntimeError("op said no for " + ref)
        return {URL_REF: url, KEY_REF: SECRET_KEY}[ref]
    config = {agent: {"url_ref": URL_REF, "key_ref": KEY_REF, "timeout_s": timeout_s}}
    if desks is not None:
        config[agent]["desks"] = desks
    return REAL_WAKER(resolver=resolver, config=lambda: config, log=relay.log, clock=clock)


def deliver(subject, waker, text="hello main-desk", sender=None):
    hdr = dict(HDR, **({"From": sender} if sender else {}))
    m, nc = FakeMsg(subject, text, hdr), FakeNC()
    out = io.StringIO()
    with mock.patch.object(relay, "WAKER", waker), contextlib.redirect_stdout(out):
        run(relay.handle(nc, m, lib.DOORS, lib.PairLimiter()))
    logs = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
    return m, nc, logs, out.getvalue()


def inbox_texts(agent, project):
    return [p.read_text() for p in lib.inbox_list(agent, project)]


# ------------------------------------------------------------------ no config
print("no wake entry")
hook = Hook()
m, nc, logs, raw = deliver("post.main-desk.grokbot-desktop", wake.Waker(config=lambda: {}))
detail = nc.sent[0][1]["detail"]
check("delivered exactly as before: the 'cannot be woken' note, nothing about a webhook", m.acked and nc.sent[0][1]["status"] == "delivered"
      and "cannot be woken" in detail and "webhook" not in detail, detail)
check("no call was made and no wake line is logged", not hook.calls and not [l for l in logs if l["event"].startswith("wake_")], str(logs))
check("the real default config (no wake.json in the support folder) also means no wake", wake.load_wake() == {} and not wake.Waker().configured(GBD))

# ------------------------------------------------------------------ configured
print("a delivery with a wake entry")
refs = []
waker = make_waker(hook.url, refs)
m, nc, logs, raw = deliver("post.ring.grokbot-desktop", waker)
detail = nc.sent[0][1]["detail"]
check("the webhook was called exactly once", len(hook.calls) == 1, str(len(hook.calls)))
call = hook.calls[0] if hook.calls else {"json": {}, "headers": {}, "path": ""}
check("with the right JSON body (event, to, from, inbox_count, ts)", call["json"].get("event") == "mail" and call["json"].get("to") == "grokbot-desktop@ring"
      and call["json"].get("from") == "claude@claude" and call["json"].get("inbox_count") == 1 and call["json"].get("ts", "")[:2] == "20", str(call["json"]))
check("with the sender key header and a JSON content type, on the URL's own path", call["headers"].get("X-Postbox-Key") == SECRET_KEY
      and call["headers"].get("Content-Type") == "application/json" and call["path"] == "/hook/abc123", str(call))
check("the sender is told: delivered ... and its wake webhook was called", detail.startswith("delivered to grokbot-desktop@ring (inbox)") and detail.endswith(" and its wake webhook was called"), detail)
check("the message is in the inbox (the source of truth) and was acknowledged", m.acked and len(inbox_texts(GBD, "ring")) == 1)
check("the log says wake_sent, and neither the URL, the key, the references nor the port appear in it", any(l["event"] == "wake_sent" for l in logs)
      and SECRET_KEY not in raw and "/hook/abc123" not in raw and str(hook.port) not in raw and "op://" not in raw, raw)
check("the secrets were fetched through the resolver by reference (url and key, once each)", sorted(refs) == sorted([URL_REF, KEY_REF]), str(refs))
m, nc, logs, raw = deliver("post.ring.grokbot-desktop", waker, text="second")
check("a second delivery calls again, with inbox_count 2, and does not ask the resolver again (kept in memory)",
      len(hook.calls) == 2 and hook.calls[1]["json"]["inbox_count"] == 2 and len(refs) == 2, f"{len(hook.calls)} {refs}")
m, nc, logs, raw = deliver("post.other.grokbot-desktop", waker, text="other project")
check("another project's inbox rings with its own address", hook.calls[-1]["json"]["to"] == "grokbot-desktop@other" and hook.calls[-1]["json"]["inbox_count"] == 1)
check("the body names the sender's agent and project as separate fields (claude@claude)",
      hook.calls[0]["json"].get("from_agent") == "claude" and hook.calls[0]["json"].get("from_project") == "claude", str(hook.calls[0]["json"]))
before_calls = len(hook.calls)
for sender, want_agent, want_project in [("claude@project-a", "claude", "project-a"), ("Codex@Project-C@mac-b", "codex", "project-c"),
                                         ("wake-test@mac-a", "wake-test", "mac-a"), ("nosender", "nosender", "")]:
    waker.wake("grokbot-desktop", "ring", sender, 1)
    sent = hook.calls[-1]["json"]
    check(f"sender {sender!r} -> from_agent {want_agent!r}, from_project {want_project!r}, from kept verbatim",
          sent.get("from_agent") == want_agent and sent.get("from_project") == want_project and sent.get("from") == sender, str(sent))
check("those four calls reached the webhook", len(hook.calls) == before_calls + 4, str(len(hook.calls)))
check("with no desks configured the body carries desk null", all(c["json"].get("desk", "missing") is None for c in hook.calls[before_calls:]), str(hook.calls[-1]["json"]))
DESKS = {"project-a": "Project A Desk", "project-c": "Project C Desk", "project-g-full": "Project G Desk"}
dw = make_waker(hook.url, desks=DESKS)
for sender, want in [("claude@project-a", "Project A Desk"), ("codex@project-a", "Project A Desk"), ("claude@project-c@mac-b", "Project C Desk"),
                     ("claude@project-g-full", "Project G Desk"), ("claude@unmapped", None), ("nosender", None)]:
    dw.wake("grokbot-desktop", "ring", sender, 1)
    check(f"desks configured: sender {sender!r} -> desk {want!r}", hook.calls[-1]["json"].get("desk", "missing") == want, str(hook.calls[-1]["json"]))
bad_dw = make_waker(hook.url, desks=["not", "a", "dict"])
bad_dw.wake("grokbot-desktop", "ring", "claude@project-a", 1)
check("a damaged desks entry (not a map) means desk null, never a crash", hook.calls[-1]["json"].get("desk", "missing") is None, str(hook.calls[-1]["json"]))
m, nc, logs, raw = deliver("post.ring.grokbot-desktop", dw, text="desk in log")
check("the log line for a wake names the desk (a name, no URL or key)", any(l["event"] == "wake_sent" and "desk" in l for l in logs) and SECRET_KEY not in raw, raw)
hook.stop()

# ------------------------------------------------------------------ a Desk with its own webhook
print("per-Desk wake entries (agent@project keys)")
main_desk_hook, desk_hook = Hook(), Hook()
D_URL, D_KEY = "op://<vault>/<desk-a>/url", "op://<vault>/<desk-a>/key"
vals = {URL_REF: main_desk_hook.url, KEY_REF: SECRET_KEY, D_URL: desk_hook.url, D_KEY: "desk-key-xyz"}
cfg = {wake.wake_key("grokbot-desktop"): {"url_ref": URL_REF, "key_ref": KEY_REF, "timeout_s": 5},
       wake.wake_key("grokbot-desktop", "Project-A"): {"url_ref": D_URL, "key_ref": D_KEY, "timeout_s": 5}}
dwaker = REAL_WAKER(resolver=lambda ref: vals[ref], config=lambda: cfg, log=relay.log)
check("keys: 'agent' and 'agent@project' are told apart and normalised", wake.wake_key("GrokBot-Desktop") == "grokbot-desktop" and wake.wake_key("grokbot-desktop", "Project C") == "grokbot-desktop@project-c")
check("a project with its own entry is configured with its own webhook", dwaker.has_own_entry("grokbot-desktop", "project-a") and not dwaker.has_own_entry("grokbot-desktop", "main-desk"))
dwaker.wake("grokbot-desktop", "project-a", "claude@project-a", 1)
check("mail for grokbot-desktop@project-a rings the Desk's own webhook with its own key, and NOT the main Desk's",
      len(desk_hook.calls) == 1 and len(main_desk_hook.calls) == 0 and desk_hook.calls[0]["headers"].get("X-Postbox-Key") == "desk-key-xyz", f"{len(desk_hook.calls)} {len(main_desk_hook.calls)}")
check("the Desk's body names it as the recipient", desk_hook.calls[0]["json"].get("to") == "grokbot-desktop@project-a", str(desk_hook.calls[0]["json"]))
dwaker.wake("grokbot-desktop", "project-e", "claude@project-e", 1)
check("a project without its own entry falls back to the agent entry (the main Desk)", len(main_desk_hook.calls) == 1 and len(desk_hook.calls) == 1, f"{len(main_desk_hook.calls)} {len(desk_hook.calls)}")
check("an agent with neither entry is not configured", not dwaker.configured("codex", "project-a") and dwaker.configured("grokbot-desktop", "project-e"))
import json as _json, tempfile as _tf
from pathlib import Path
with _tf.TemporaryDirectory() as td, mock.patch.object(wake, "wake_file", lambda: Path(td) / "wake.json"):
    (Path(td) / "wake.json").write_text(_json.dumps({"grokbot-desktop": cfg["grokbot-desktop"], "grokbot-desktop@Project-A": cfg["grokbot-desktop@project-a"],
                                                      "grokbot-desktop@bad": {"url_ref": "https://pasted", "key_ref": "op://<x>"}}))
    loaded = wake.load_wake()
check("wake.json is read with agent@project keys; an entry that is not two 1Password references is dropped", sorted(loaded) == ["grokbot-desktop", "grokbot-desktop@project-a"], str(sorted(loaded)))
main_desk_hook.stop(); desk_hook.stop()

# ------------------------------------------------------------------ an entry with a whole header line (header_ref)
print("header_ref: the Desk's own header line is sent as it is")
hh = Hook()
H_URL, H_HDR = "op://<vault>/<desk-h>/url", "op://<vault>/<desk-h>/header"
hvals = {H_URL: hh.url, H_HDR: "Authorization: Bearer desk-secret-777"}
hcfg = {wake.wake_key("grokbot-desktop", "project-f"): {"url_ref": H_URL, "header_ref": H_HDR, "timeout_s": 5}}
hw = REAL_WAKER(resolver=lambda ref: hvals[ref], config=lambda: hcfg, log=relay.log)
res = hw.wake("grokbot-desktop", "project-f", "claude@project-f", 1)
sent_h = {k.lower(): v for k, v in hh.calls[-1]["headers"].items()} if hh.calls else {}
check("the header line is sent exactly (name and value) and the call succeeds", res.ok and sent_h.get("authorization") == "Bearer desk-secret-777", str(res))
check("no postbox key header and no second Authorization are added when a header line is given", "x-postbox-key" not in sent_h and sent_h.get("content-type") == "application/json", str(sent_h.keys()))
with _tf.TemporaryDirectory() as td, mock.patch.object(wake, "wake_file", lambda: Path(td) / "wake.json"):
    (Path(td) / "wake.json").write_text(_json.dumps({"grokbot-desktop@a": {"url_ref": H_URL, "header_ref": H_HDR}, "grokbot-desktop@b": {"url_ref": H_URL}}))
    hl = wake.load_wake()
check("an entry may have header_ref instead of key_ref; one with neither is dropped", sorted(hl) == ["grokbot-desktop@a"], str(sorted(hl)))
n_before = len(hh.calls)
for bad in ["no colon here", "Content-Type: text/plain", "Bad Name: x", "Authorization:   "]:
    hvals[H_HDR] = bad
    hw2 = REAL_WAKER(resolver=lambda ref: hvals[ref], config=lambda: hcfg, log=relay.log)
    r = hw2.wake("grokbot-desktop", "project-f", "claude@project-f", 1)
    check(f"a bad header line {bad!r} fails as bad_header and calls nothing", (not r.ok) and r.reason in ("bad_header", "secret_unavailable") and len(hh.calls) == n_before, str(r))
m, nc, logs, raw = deliver("post.project-f.grokbot-desktop", REAL_WAKER(resolver=lambda ref: {H_URL: hh.url, H_HDR: "Authorization: Bearer desk-secret-777"}[ref], config=lambda: hcfg, log=relay.log), text="x")
check("the log never contains the header value", "desk-secret-777" not in raw, raw[-300:])
hh.stop()

# ------------------------------------------------------------------ Desk mail sent to the main Desk goes to the Desk
print("redirect: mail to grokbot-desktop@main-desk from a project with its own Desk")
rf, rd = Hook(), Hook()
rvals = {URL_REF: rf.url, KEY_REF: SECRET_KEY, "op://<vault>/<desk-c>/url": rd.url, "op://<vault>/<desk-c>/header": "Authorization: Bearer desk-key-rr"}
rcfg = {wake.wake_key("grokbot-desktop"): {"url_ref": URL_REF, "key_ref": KEY_REF},
        wake.wake_key("grokbot-desktop", "project-c"): {"url_ref": "op://<vault>/<desk-c>/url", "header_ref": "op://<vault>/<desk-c>/header"}}
rw = REAL_WAKER(resolver=lambda ref: rvals[ref], config=lambda: rcfg, log=relay.log)
with mock.patch.object(relay, "WAKER", rw):
    check("project-c mail addressed to the main Desk is redirected to project-c", relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@project-c") == "project-c")
    check("a project without its own Desk stays with the main Desk", relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@claude") is None)
    check("mail already addressed to a Desk is left alone", relay.redirect_desk_mail("grokbot-desktop", "project-c", "claude@project-c") is None)
    check("mail from another Mac (agent@project@machine) is never redirected", relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@project-c@mac-b") is None)
    check("mail to other agents is never redirected", relay.redirect_desk_mail("claude", "main-desk", "claude@project-c") is None)
    check("a typo project (grokbot-desktop@grok-desktop) from a Desk project goes to the sender's Desk", relay.redirect_desk_mail("grokbot-desktop", "grok-desktop", "codex@project-c") == "project-c")
    check("a typo from a project WITHOUT a Desk is left alone", relay.redirect_desk_mail("grokbot-desktop", "grok-desktop", "claude@claude") is None)
    check("mail to ANOTHER real Desk is left alone (on purpose)", relay.redirect_desk_mail("grokbot-desktop", "project-c", "claude@project-c") is None
          and relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@project-c") == "project-c")
    check("the main Desk's own sends are never redirected", relay.redirect_desk_mail("grokbot-desktop", "main-desk", "grokbot-desktop@main-desk") is None)
    check("the old hard-coded general-Desk name is gone: a project called main-desk is treated like any other (no own entry: nothing to redirect to)",
          relay.redirect_desk_mail("grokbot-desktop", "project-c", "claude@main-desk") is None)
    rcfg["grokbot-desktop"]["desk_sender_projects"] = ["Project-C"]
    check('"desk_sender_projects" lists projects whose mail is never redirected (matched after cleaning the name)',
          relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@project-c") is None)
    rcfg["grokbot-desktop"]["desk_sender_projects"] = "project-c"
    check("a desk_sender_projects that is not a list is ignored, not read letter by letter", relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@project-c") == "project-c")
    del rcfg["grokbot-desktop"]["desk_sender_projects"]
    check("with no desk_sender_projects (the default) the same mail is redirected", relay.redirect_desk_mail("grokbot-desktop", "main-desk", "claude@project-c") == "project-c")
m, nc, logs, raw = deliver("post.main-desk.grokbot-desktop", rw, text="21sk", sender="claude@project-c")
detail = nc.sent[0][1]["detail"]
check("the mail lands in the project-c Desk inbox, not the main Desk's", len(inbox_texts(GBD, "project-c")) == 1 and "21sk" in inbox_texts(GBD, "project-c")[0]
      and not any("21sk" in t for t in inbox_texts(GBD, "main-desk")))
check("the Desk's own webhook was rung, with its own header, and the main Desk's was not", len(rd.calls) == 1 and len(rf.calls) == 0 and {k.lower(): v for k, v in rd.calls[0]["headers"].items()}.get("authorization") == "Bearer desk-key-rr", f"{len(rd.calls)} {len(rf.calls)}")
check("the sender is told it went to the project's own Desk", "grokbot-desktop@project-c" in detail and "instead of grokbot-desktop@main-desk" in detail and detail.endswith(" and its wake webhook was called"), detail)
check("the log has a desk_redirect line and the wake line says via desk", any(l["event"] == "desk_redirect" for l in logs) and any(l["event"] == "wake_sent" and l.get("via") == "desk" for l in logs), raw[-300:])
rcfg["grokbot-desktop"]["desk_redirect"] = False
m, nc, logs, raw = deliver("post.main-desk.grokbot-desktop", rw, text="stays with main-desk", sender="claude@project-c")
check('with "desk_redirect": false in the agent entry the mail stays with the main Desk', any("stays with main-desk" in t for t in inbox_texts(GBD, "main-desk")) and len(rf.calls) == 1 and len(inbox_texts(GBD, "project-c")) == 1, str(len(rf.calls)))
rf.stop(); rd.stop()

# ------------------------------------------------------------------ webhook fails
print("a failing webhook")
bad = Hook(status=500)
m, nc, logs, raw = deliver("post.fail.grokbot-desktop", make_waker(bad.url), text="mail during a bad webhook")
detail = nc.sent[0][1]["detail"]
check("still delivered and acknowledged, the message is in the inbox", m.acked and not m.naked and nc.sent[0][1]["status"] == "delivered" and len(inbox_texts(GBD, "fail")) == 1, str(nc.sent))
check("one attempt only (no retry)", len(bad.calls) == 1, str(len(bad.calls)))
check("the sender is told the wake failed and that the inbox will be read on the next turn",
      detail.endswith("(wake webhook failed: http_500; it will read the inbox on its next turn)"), detail)
check("wake_failed is logged with a reason and without the URL or key", any(l["event"] == "wake_failed" and l.get("reason") == "http_500" for l in logs)
      and SECRET_KEY not in raw and "/hook/abc123" not in raw, raw)
bad.stop()
slow = Hook(delay=3.0)
t0 = time.monotonic()
m, nc, logs, raw = deliver("post.slow.grokbot-desktop", make_waker(slow.url, timeout_s=1), text="mail during a hung webhook")
took = time.monotonic() - t0
check("a webhook that hangs is cut off at its timeout, delivery still succeeds", m.acked and nc.sent[0][1]["status"] == "delivered" and took < 2.5, f"{took:.1f}s")
check("and says timeout", "wake webhook failed: timeout;" in nc.sent[0][1]["detail"] and any(l["event"] == "wake_failed" and l.get("reason") == "timeout" for l in logs), nc.sent[0][1]["detail"])
slow.stop()
dead = Hook()
dead_url = dead.url
dead.stop()
m, nc, logs, raw = deliver("post.refused.grokbot-desktop", make_waker(dead_url), text="nobody listening")
check("a closed port is a failure with a class, not a crash, and the mail is still delivered", m.acked and "wake webhook failed: connection_refused;" in nc.sent[0][1]["detail"], nc.sent[0][1]["detail"])

# ------------------------------------------------------------------ secrets cannot be read
print("the secret cannot be resolved")
live = Hook()
now = [1000.0]
calls = []
waker = make_waker(live.url, calls, fail=True, clock=lambda: now[0])
m, nc, logs1, raw1 = deliver("post.nosecret.grokbot-desktop", waker, text="first")
m2, nc2, logs2, raw2 = deliver("post.nosecret.grokbot-desktop", waker, text="second")
check("no call is made, and both messages are delivered and stored", not live.calls and nc.sent[0][1]["status"] == "delivered" and nc2.sent[0][1]["status"] == "delivered"
      and len(inbox_texts(GBD, "nosecret")) == 2)
check("the sender hears secret_unavailable", "(wake webhook failed: secret_unavailable; it will read the inbox on its next turn)" in nc.sent[0][1]["detail"], nc.sent[0][1]["detail"])
check("the failure is logged once, not once per delivery", len([l for l in logs1 + logs2 if l["event"] == "wake_failed"]) == 1, str(logs1 + logs2))
check("the reference is not asked again inside the retry window (a down 1Password does not stall every delivery)", len(calls) == 1, str(calls))
check("neither the references nor the resolver's error text reach the log", "op://" not in raw1 + raw2 and "op said no" not in raw1 + raw2, raw1)
now[0] += wake.RESOLVE_RETRY_S + 1
deliver("post.nosecret.grokbot-desktop", waker, text="third")
check("after the retry window it asks again", len(calls) == 2, str(calls))
good = make_waker(live.url, [], clock=lambda: now[0])
deliver("post.nosecret.grokbot-desktop", good, text="fourth")
check("and once the secret is stored the very next delivery rings", len(live.calls) == 1)

# ------------------------------------------------------------------ IPv4 only
print("IPv4 only")
seen = {"lookups": [], "sockets": []}


class SpySocket(socket.socket):
    def __init__(self, family=-1, *a, **kw):
        seen["sockets"].append(family)
        super().__init__(family, *a, **kw)


def spy_lookup(host, port, family=0, *a, **kw):
    seen["lookups"].append(family)
    return socket.getaddrinfo(host, port, family, *a, **kw)


proxy = types.SimpleNamespace(**{k: getattr(socket, k) for k in dir(socket) if not k.startswith("__")})
proxy.socket, proxy.getaddrinfo = SpySocket, spy_lookup
with mock.patch.object(wake, "socket", proxy):
    result = make_waker(live.url).wake(GBD, "main-desk", "claude@claude", 1)
check("the host name is looked up as IPv4 only and the socket is an IPv4 socket", result.ok and seen["lookups"] == [socket.AF_INET] and seen["sockets"] == [socket.AF_INET], str(seen))
check("so a name that also resolves to ::1 still reaches the IPv4-only server (localhost, server bound to 127.0.0.1)", result.ok and len(live.calls) == 2, str(result))


def v6_only(host, port, family=0, *a, **kw):
    if family == socket.AF_INET:
        raise socket.gaierror(8, "no IPv4")
    return socket.getaddrinfo(host, port, family, *a, **kw)


proxy.getaddrinfo = v6_only
with mock.patch.object(wake, "socket", proxy):
    result = make_waker(live.url).wake(GBD, "main-desk", "claude@claude", 1)
check("a host with only an IPv6 address is a clean failure (dns), never a fallback to IPv6", not result.ok and result.reason == "dns" and len(live.calls) == 2, str(result))
check("plain http to anything but this Mac is refused (bad_url)", make_waker("http://example.com/x").wake(GBD, "main-desk", "a@b", 1).reason == "bad_url")
check("and so is a URL that is not http(s) at all", make_waker("ftp://example.com/x").wake(GBD, "main-desk", "a@b", 1).reason == "bad_url")
live.stop()

# ------------------------------------------------------------------ other doors and the config file
print("other doors and the config file")
other = Hook()
m, nc, logs, raw = deliver("post.main-desk.codex", make_waker(other.url, agent="codex"), text="to a codex that is not there")
check("only inbox-only doors ring: a wake entry named codex is never called", not other.calls)
other.stop()
with mock.patch.object(relay, "WAKER", make_waker("http://localhost:1/", agent="claude")):
    check("ring_wake says nothing for a door that is not inbox-only even when an entry exists", run(relay.ring_wake(lib.DOORS["claude"], "claude", "main-desk", "a@b")) == "")
os.makedirs(os.environ["POSTBOX_SUPPORT_DIR"], exist_ok=True)
path = wake.wake_file()
path.write_text("not json")
check("a damaged wake.json means no wake, not a crash", wake.load_wake() == {})
path.write_text(json.dumps({GBD: {"url_ref": URL_REF, "key_ref": KEY_REF, "timeout_s": 5}, "bad": {"url_ref": "https://plain-secret.example/x", "key_ref": KEY_REF}, "worse": "x"}))
check("only entries made of two 1Password references are kept (a pasted URL is dropped)", list(wake.load_wake()) == [GBD], str(wake.load_wake()))
check("the timeout is clamped", make_waker("http://localhost:1/", timeout_s="x").wake(GBD, "f", "a@b", 1).reason in ("connection_refused", "network"))

# ------------------------------------------------------------------ post who
print("post who")
os.environ["POSTBOX_INBOX_DIR"] = os.path.join(TMP, "inbox-who")
with mock.patch.object(lib, "claude_sessions", lambda: []), mock.patch.object(lib, "codex_sessions", lambda: []), mock.patch.object(lib, "grok_sessions", lambda: []), \
        mock.patch.object(lib, "my_identity", return_value=None):
    lib.inbox_put(GBD, "main-desk", "one")
    code, out, _ = cli(["post", "who"])
    check("with a wake entry the line says wake: configured (and keeps its old text)", "(inbox, 1 waiting; cannot be woken)" in out and "wake: configured" in out and "wake: not configured" not in out, out)
    path.write_text("{}")
    code, out, _ = cli(["post", "who"])
    check("without one it says wake: not configured", "(inbox, 1 waiting; cannot be woken)" in out and "wake: not configured" in out, out)

# ------------------------------------------------------------------ post wake-test
print("post wake-test")
path.write_text(json.dumps({GBD: {"url_ref": URL_REF, "key_ref": KEY_REF}}))
tester = Hook()
before = len(lib.inbox_list(GBD, "main-desk"))
with mock.patch.object(wake, "Waker", lambda: make_waker(tester.url)):
    code, out, err = cli(["post", "wake-test", f"{GBD}@main-desk"])
check("wake-test prints sent and exits 0", code is None and out.strip() == "wake: sent to grokbot-desktop@main-desk (HTTP 200)", f"{code} {out} {err}")
check("the call carries event test, the current inbox count and the key", len(tester.calls) == 1 and tester.calls[0]["json"]["event"] == "test"
      and tester.calls[0]["json"]["inbox_count"] == before and tester.calls[0]["headers"].get("X-Postbox-Key") == SECRET_KEY, str(tester.calls))
check("and stores nothing in the inbox", len(lib.inbox_list(GBD, "main-desk")) == before)
tester.status = 503
with mock.patch.object(wake, "Waker", lambda: make_waker(tester.url)):
    code, out, err = cli(["post", "wake-test", f"{GBD}@main-desk"])
check("a failing webhook prints failed with the reason and exits 2", code == 2 and "wake: failed for grokbot-desktop@main-desk (http_503)" in err and out == "", f"{code} {out} {err}")
check("the output never shows the URL or key", SECRET_KEY not in out + err and "/hook/abc123" not in out + err)
with mock.patch.object(wake, "Waker", lambda: make_waker(tester.url, agent="someone-else")):
    code, out, err = cli(["post", "wake-test", f"{GBD}@main-desk"])
check("an agent with no entry says so and exits 2", code == 2 and "no wake entry for grokbot-desktop" in err, f"{code} {err}")
code, out, err = cli(["post", "wake-test", GBD])
check("an address without a project is refused in words", code == 2 and "needs the project" in err, err)
code, out, err = cli(["post", "wake-test"])
check("so is no address at all", code == 2 and "takes one address" in err, err)
# a wake entry for some other agent must never be rung through wake-test, even with the grokbot-desktop door on
path.write_text(json.dumps({GBD: {"url_ref": URL_REF, "key_ref": KEY_REF}, "codex": {"url_ref": URL_REF, "key_ref": KEY_REF}}))
tester.calls.clear()
tester.status = 200
for other in ("codex@main-desk", "claude@main-desk", "grok@main-desk"):
    with mock.patch.object(wake, "Waker", lambda: make_waker(tester.url, agent="codex")):
        code, out, err = cli(["post", "wake-test", other])
    check(f"wake-test {other}: refused, only grokbot-desktop can be rung", code == 2 and "only rings the grokbot-desktop door" in err and out == "", f"{code} {out} {err}")
check("and none of them made a webhook call or read an inbox", tester.calls == [], str(tester.calls))
tester.stop()

# ------------------------------------------------------------------ which command resolves a 1Password reference
print("secret command")
_old = os.environ.pop("POSTBOX_SECRET_CMD", None)
with mock.patch.object(wake.Path, "home", classmethod(lambda cls: Path(TMP) / "no-such-home")):
    check("no harness helper on this Mac and no override: the 1Password command line tool (`op read`) is used", wake.secret_command() == ["op", "read"], str(wake.secret_command()))
helper_home = Path(TMP) / "helper-home"
(helper_home / ".claude" / "bin").mkdir(parents=True)
(helper_home / ".claude" / "bin" / "op-cached").write_text("#!/bin/sh\necho from-helper\n")
with mock.patch.object(wake.Path, "home", classmethod(lambda cls: helper_home)):
    check("an op-cached helper in ~/.claude/bin is used when it exists", wake.secret_command() == [str(helper_home / ".claude" / "bin" / "op-cached")])
os.environ["POSTBOX_SECRET_CMD"] = f"{sys.executable} -c 'import sys; print(\"value-for-\" + sys.argv[-1])'"
check("POSTBOX_SECRET_CMD wins, and the reference is its last argument", wake.op_cached("op://<vault>/<item>/<field>") == "value-for-op://<vault>/<item>/<field>")
os.environ["POSTBOX_SECRET_CMD"] = f"{sys.executable} -c 'import sys; sys.exit(3)'"
try:
    wake.op_cached("op://<vault>/<item>/<field>")
    check("a failing secret command raises SecretUnavailable", False)
except wake.SecretUnavailable as exc:
    check("a failing secret command raises SecretUnavailable without the reference in the error", "op://" not in str(exc) and "exit 3" in str(exc), str(exc))
if _old is None:
    os.environ.pop("POSTBOX_SECRET_CMD", None)
else:
    os.environ["POSTBOX_SECRET_CMD"] = _old

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
