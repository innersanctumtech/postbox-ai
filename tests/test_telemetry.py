#!/usr/bin/env python3
"""Tests for the post box telemetry (postbox/telemetry.py and its wiring into post.py, relay.py, lib.py, selfcheck.py).

Run: .venv/bin/python tests/test_telemetry.py

Every event is written to a throwaway spool folder (POSTBOX_TELEMETRY_SPOOL); nothing here touches a real spool, the real
support folder or any database. The central rule is the privacy one: a secret-looking message body must never appear anywhere in an
emitted event or in the fallback file, on the sending side, the relay side and the Grok inbox side.
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
import shutil
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)

TMP = tempfile.mkdtemp(prefix="postbox-telemetry-test-")
SPOOL = os.path.join(TMP, "spool")
SUPPORT = os.path.join(TMP, "support")
os.makedirs(SUPPORT)
os.environ.update(POSTBOX_TELEMETRY_SPOOL=SPOOL, POSTBOX_SUPPORT_DIR=SUPPORT, POSTBOX_MACHINE="mac-a",
                  POSTBOX_INBOX_DIR=os.path.join(TMP, "inbox"))
for name in ("POSTBOX_CALLER", "POSTBOX_AS"):
    os.environ.pop(name, None)
os.environ["POSTBOX_TELEMETRY"] = "1"   # telemetry is OFF unless asked for; this suite is about telemetry, so it asks

import lib  # noqa: E402
import post  # noqa: E402
import relay  # noqa: E402
import selfcheck  # noqa: E402
import telemetry  # noqa: E402

PASS = FAIL = 0
SECRET = "sk-live-TESTTOKEN9f8e7d6c5b4a password=hunter2horsebattery"
SECRET_PARTS = ("sk-live-TESTTOKEN9f8e7d6c5b4a", "hunter2horsebattery", "password=")


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def fresh():
    """Empty spool, zeroed counters, clean relay state."""
    shutil.rmtree(SPOOL, ignore_errors=True)
    telemetry.reset()
    relay.STATS.reset()
    relay.QUEUED.clear()
    relay.ERR_LAST.clear()
    lib.IDENTITY_TRACE.update(method="none", failed=[])


def events(kind=None):
    """Every event in the spool, oldest first, as (event_type, payload-with-metadata-flattened-under 'm')."""
    out = []
    for path in Path(SPOOL).glob("post-*.ndjson") if Path(SPOOL).is_dir() else []:
        env = json.loads(path.read_text())
        out.append(env)
    out.sort(key=lambda e: e["ts"])
    return [e for e in out if kind is None or e["event_type"] == kind]


def meta(event):
    return event["payload"]["metadata"]


def every_byte_stored():
    """Everything telemetry left on disk: spool files and the fallback file, as one string."""
    blob = ""
    for root in (SPOOL, SUPPORT):
        for path in Path(root).rglob("*"):
            if path.is_file() and (path.name.startswith("post-") or path.name == "telemetry-fallback.ndjson"):
                blob += path.read_text()
    return blob


# ================================================================= the writer itself
print("-- writer")
fresh()
ok = telemetry.emit("post_send", "post-cli", status="success", duration_ms=12.34, error_message=None, result="delivered", door="claude")
files = [p.name for p in Path(SPOOL).iterdir()]
ev = events()[0]
check("emit writes exactly one post-*.ndjson file and no temp file", ok and len(files) == 1 and files[0].startswith("post-") and files[0].endswith(".ndjson"), files)
check("the envelope has the fields replay-spool.sh and the table need",
      {"event_id", "event_type", "session_id", "project_path", "ts", "payload"} <= set(ev) and ev["event_type"] == "post_send"
      and ev["payload"]["component_type"] == "post" and ev["payload"]["component_name"] == "post-cli" and ev["payload"]["source"] == "postbox"
      and ev["payload"]["duration_ms"] == 12.34 and ev["payload"]["status"] == "success")
check("every event carries host, os user, identity and schema (two Macs stay distinguishable)",
      meta(ev)["host"] == "mac-a" and meta(ev)["identity"] == f"{meta(ev)['os_user']}-mac-a" and meta(ev)["schema"] == 1 and meta(ev)["door"] == "claude")
check("the timestamp is an aware ISO time with microseconds", datetime.fromisoformat(ev["ts"]).tzinfo is not None and "." in ev["ts"])

with mock.patch.dict(os.environ, {"POSTBOX_MACHINE": "mac-b"}):
    telemetry.emit("post_send", "post-cli")
check("another Mac's name changes the host and identity", {meta(e)["host"] for e in events()} == {"mac-a", "mac-b"}
      and [meta(e)["identity"].endswith("-mac-b") for e in events()].count(True) == 1)

fresh()
stamps = []
for _ in range(200):
    telemetry.emit("post_send", "post-cli")
stamps = [e["ts"] for e in events()]
check("200 events in a row never share a timestamp (the table's unique key includes it)", len(set(stamps)) == 200)

fresh()
for off in ("0", "false", "OFF", "no"):
    with mock.patch.dict(os.environ, {"POSTBOX_TELEMETRY": off}):
        wrote = telemetry.emit("post_send", "post-cli")
        check(f"kill switch POSTBOX_TELEMETRY={off} writes nothing and says so", wrote is False and not Path(SPOOL).exists())
check("the kill switch is counted", telemetry.stats()["disabled"] == 4)
fresh()
with mock.patch.dict(os.environ):
    os.environ.pop("POSTBOX_TELEMETRY")
    check("with POSTBOX_TELEMETRY unset, telemetry is OFF (the public default): nothing is written", telemetry.enabled() is False
          and telemetry.emit("post_send", "post-cli") is False and not Path(SPOOL).exists())
for on in ("1", "true", "ON", "yes"):
    with mock.patch.dict(os.environ, {"POSTBOX_TELEMETRY": on}):
        check(f"POSTBOX_TELEMETRY={on} turns it on", telemetry.enabled() is True)

fresh()
blocker = os.path.join(TMP, "afile")
Path(blocker).write_text("x")
class Unprintable:
    def __repr__(self):
        raise RuntimeError("do not call me")
with mock.patch.dict(os.environ, {"POSTBOX_TELEMETRY_SPOOL": os.path.join(blocker, "sub"), "POSTBOX_SUPPORT_DIR": os.path.join(blocker, "sup")}):
    try:
        result = telemetry.emit("post_send", "post-cli")
        raised = False
    except BaseException:  # noqa: BLE001
        result, raised = None, True
check("emit never raises, even when the spool and the fallback are both unwritable", not raised and result is False and telemetry.stats()["failed"] == 1)
fresh()
telemetry.emit("post_send", "post-cli", weird=Unprintable(), when=datetime.now(), nan=float("nan"), nested={"a": {"b": {"c": {"d": 1}}}})
m = meta(events()[0])
check("an object that cannot be printed is recorded by class name only, nan becomes null, deep nesting is cut",
      m["weird"] == "Unprintable" and m["nan"] is None and m["when"] == "datetime" and m["nested"]["a"]["b"] is None)

# fallback file when the harness spool does not exist
fresh()
fake_home = os.path.join(TMP, "home-without-harness")
os.makedirs(fake_home, exist_ok=True)
with mock.patch.dict(os.environ, {"HOME": fake_home}), mock.patch.object(telemetry, "under_test", lambda: False):
    os.environ.pop("POSTBOX_TELEMETRY_SPOOL")
    check("no harness spool folder: spool_dir() is None", telemetry.spool_dir() is None)
    wrote = telemetry.emit("post_send", "post-cli", result="delivered")
fallback = Path(SUPPORT) / "telemetry-fallback.ndjson"
check("...so the event goes to the local fallback file instead", wrote and fallback.is_file() and json.loads(fallback.read_text().splitlines()[0])["event_type"] == "post_send"
      and telemetry.stats()["fallback"] == 1)
os.makedirs(os.path.join(fake_home, ".claude", "state", "spool"))
with mock.patch.dict(os.environ, {"HOME": fake_home}), mock.patch.object(telemetry, "under_test", lambda: False):
    os.environ.pop("POSTBOX_TELEMETRY_SPOOL")
    where = telemetry.spool_dir()
check("with the harness spool folder present, events go to its queue folder", where == Path(fake_home) / ".claude" / "state" / "spool" / "queue")
os.environ["POSTBOX_TELEMETRY_SPOOL"] = SPOOL
fallback.unlink()
with mock.patch.object(telemetry, "FALLBACK_MAX_BYTES", 10), mock.patch.object(telemetry, "spool_dir", lambda: None):
    first = telemetry.emit("post_send", "post-cli")
    second = telemetry.emit("post_send", "post-cli")
check("the fallback file is capped: past the cap events are refused, not written forever", first is True and second is False)
fallback.unlink(missing_ok=True) if sys.version_info >= (3, 8) else None

with mock.patch.object(telemetry, "under_test", lambda: True), mock.patch.dict(os.environ, {"HOME": fake_home}):
    os.environ.pop("POSTBOX_TELEMETRY_SPOOL")
    os.environ.pop("POSTBOX_SUPPORT_DIR")
    check("under test and not told where, nothing may go to the real spool or the real support folder",
          telemetry.spool_dir() is None and telemetry._write_fallback("x\n") is False)
os.environ["POSTBOX_TELEMETRY_SPOOL"], os.environ["POSTBOX_SUPPORT_DIR"] = SPOOL, SUPPORT

fresh()
started = time.monotonic()
for _ in range(100):
    telemetry.emit("post_send", "post-cli", result="delivered", door="claude", connect_ms=3.2)
mean_ms = (time.monotonic() - started) / 100 * 1000
check(f"one event costs well under 5 ms (measured {mean_ms:.2f} ms)", mean_ms < 5.0)


# ================================================================= small helpers
print("-- helpers")
def tmp_link(name):
    Path(SUPPORT, "link.json").write_text(json.dumps({"machine": name, "peers": {}}))

for label, env, link, host in (("POSTBOX_MACHINE wins", "Mac Mini", None, None), ("link.json is next", None, "Mac-B", None), ("then the host name", None, None, "x")):
    fresh()
    for p in Path(SUPPORT).glob("link.json"):
        p.unlink()
    if link:
        tmp_link(link)
    patch = {"POSTBOX_MACHINE": env} if env else {}
    with mock.patch.dict(os.environ, patch):
        if not env:
            os.environ.pop("POSTBOX_MACHINE", None)
        check(f"machine name rule matches lib.my_machine(): {label}", telemetry.machine() == lib.my_machine(), f"{telemetry.machine()} vs {lib.my_machine()}")
for p in Path(SUPPORT).glob("link.json"):
    p.unlink()
fresh()

facts = telemetry.text_facts(SECRET)
check("text_facts gives bytes, chars and a 12 character sha256 prefix and not the text",
      facts["bytes"] == len(SECRET) and facts["chars"] == len(SECRET) and len(facts["sha256_12"]) == 12 and SECRET not in json.dumps(facts))
check("text_facts counts bytes, not characters, for non-ASCII", telemetry.text_facts("héllo")["bytes"] == 6 and telemetry.text_facts("héllo")["chars"] == 5)
with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "GROK_AGENT": "", "CODEX_THREAD_ID": SECRET}):
    hint = telemetry.harness_hint()
check("harness_hint records which variables are PRESENT and never their values",
      hint["CLAUDECODE"] is True and hint["GROK_AGENT"] is False and hint["CODEX_THREAD_ID"] is True and all(isinstance(v, bool) for v in hint.values())
      and SECRET not in json.dumps(hint))
with mock.patch.dict(os.environ, {"POSTBOX_CALLER": "Inform-Peers-Release"}):
    good = telemetry.caller()
with mock.patch.dict(os.environ, {"POSTBOX_CALLER": SECRET}):
    bad = telemetry.caller()
check("POSTBOX_CALLER is kept only when it is a short label", good == "inform-peers-release" and bad is None)
check("connect errors fall into a fixed vocabulary",
      telemetry.connect_error_class(lib.MailboxError("no other Mac called 'x' is linked", setup=True)) == "peer_not_linked"
      and telemetry.connect_error_class(lib.MailboxError("x", setup=True)) == "setup"
      and telemetry.connect_error_class(lib.MailboxError("Authorization Violation", refused=True)) == "auth_refused"
      and telemetry.connect_error_class(lib.MailboxError("NoServersError()")) == "no_servers"
      and telemetry.connect_error_class(lib.MailboxError("TimeoutError()")) == "timeout"
      and telemetry.connect_error_class(OSError("Connect call failed ('127.0.0.1', 4222)")) == "conn_refused"
      and telemetry.connect_error_class(ValueError("whatever")) == "other")
check("refusal sentences from an older relay are classified",
      telemetry.classify_refusal("stop rule: more than 30 messages from a@b to c@d in 10 minutes") == "pair_limit"
      and telemetry.classify_refusal("stop rule: more than 120 messages to c@d in 10 minutes from all senders together") == "recipient_limit"
      and telemetry.classify_refusal("message too long (40000 bytes, the limit is 32000)") == "too_long"
      and telemetry.classify_refusal("no delivery door for agent 'gemini' yet") == "no_door" and telemetry.classify_refusal("???") == "other")
check("limit_of and status_for", telemetry.limit_of("pair_limit") == "pair" and telemetry.limit_of("nobody_there") is None
      and [telemetry.status_for(r) for r in ("delivered", "queued", "refused", "error", "weird")] == ["success", "warning", "blocked", "error", "error"])
check("error_class keeps only the class name; error_class_of_text reads a repr's class",
      telemetry.error_class(OSError(SECRET)) == "OSError" and telemetry.error_class_of_text("NoServersError('x')") == "NoServersError"
      and telemetry.error_class_of_text("this Mac's login key file is damaged") == "message")


# ================================================================= privacy
print("-- privacy")
fresh()
telemetry.emit("post_send", "post-cli", body=SECRET, text=SECRET, message=SECRET, msg=SECRET, env={"X": SECRET}, headers={"From": SECRET},
               api_token=SECRET, password=SECRET, nkey_seed=SECRET, result="delivered", note=SECRET[:20])
m = meta(events()[0])
check("keys that look like content or secrets are dropped", not ({"body", "text", "message", "msg", "env", "headers", "api_token", "password", "nkey_seed"} & set(m)), sorted(m))
check("ordinary facts survive the same call", m["result"] == "delivered")
fresh()
seed = "SU" + "A" * 56
telemetry.emit("post_send", "post-cli", odd=f"before {seed} after", long="x" * 500, ctrl="a\nb\x00c")
m = meta(events()[0])
check("a value shaped like a login key (nkey seed) is replaced", seed not in every_byte_stored() and "[redacted]" in m["odd"])
check("long strings are cut and control characters flattened", len(m["long"]) <= 204 and "\n" not in m["ctrl"] and "\x00" not in m["ctrl"])
fresh()
telemetry.emit("post_send", "post-cli", error_message=f"boom {seed}")
check("the error_message column is cleaned too", seed not in every_byte_stored())


# ================================================================= the command line (post.py), end to end with a fake mailbox
print("-- post command line")
NatsTimeout = __import__("nats.errors", fromlist=["TimeoutError"]).TimeoutError


class CliNC:
    def __init__(self, answer=None, fail_publish=None):
        self.answer, self.fail_publish, self.published = answer, fail_publish, []

    def new_inbox(self):
        return "_INBOX.test1"

    async def subscribe(self, inbox):
        return self

    def jetstream(self):
        return self

    async def publish(self, subject, data, headers=None, timeout=None):
        if self.fail_publish:
            raise self.fail_publish
        self.published.append((subject, data, headers))

    async def next_msg(self, timeout=None):
        if self.answer is None:
            raise NatsTimeout()
        return types.SimpleNamespace(data=json.dumps(self.answer).encode())

    async def close(self):
        pass


SENDER = (("claude", "myproj", 4242), "found from the session post is running in")


def cli(argv, nc=None, connect_error=None, sender=SENDER, method="claude_env", sessions=None, stdin=None, peer_error=None):
    """`connect_error` fails EVERY connection; `peer_error` fails only the connection to another Mac (this Mac's own mailbox, `nc`, still works)."""
    async def fake_connect(target, timeout=3):
        if connect_error:
            raise connect_error
        if peer_error and target.label != "this Mac":
            raise peer_error
        return nc

    def fake_resolve(flag=None):
        lib.IDENTITY_TRACE.update(method=method, failed=[])
        if isinstance(sender, Exception):
            raise sender
        return sender

    out, err = io.StringIO(), io.StringIO()
    patches = [mock.patch.object(lib, "connect_fast", fake_connect), mock.patch.object(lib, "resolve_sender", fake_resolve),
               mock.patch.object(lib, "local_target", lambda: lib.Target("nats://local", None, "this Mac")),
               mock.patch.object(lib, "peer_target", lambda name: lib.Target("nats://peer", None, name)),
               mock.patch.object(lib, "all_sessions", lambda: list(sessions or [])), mock.patch.object(lib, "my_identity", lambda: SENDER[0])]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        if stdin is not None:
            stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
        try:
            post.run(["post"] + argv)
            code = 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
    return code, out.getvalue(), err.getvalue()


fresh()
nc = CliNC({"status": "delivered", "detail": "delivered to claude@other (pid 777), codex@other (pid 888)", "targets": ["claude@other (pid 777)", "codex@other (pid 888)"]})
code, out, err = cli(["claude@other", SECRET], nc)
sends = events("post_send")
m = meta(sends[0]) if sends else {}
check("a delivered send writes exactly one post_send and exits 0", code == 0 and len(sends) == 1 and len(events()) == 1, [e["event_type"] for e in events()])
check("it says who sent to whom, by which door, and how the sender was identified",
      m.get("from_addr") == "claude@myproj" and m.get("to_addr") == "claude@other" and m.get("door") == "claude" and m.get("identity_method") == "claude_env"
      and m.get("sender_pid") == 4242 and m.get("cross_machine") is False and m.get("peer") is None and m.get("explicit_as") is False)
check("result, exit code, target sessions and their pids are recorded",
      m.get("result") == "delivered" and m.get("exit_code") == 0 and m.get("targets_n") == 2 and m.get("target_pids") == [777, 888]
      and sends[0]["payload"]["status"] == "success")
check("the message is described by size and a hash, never shown",
      m.get("bytes") == len(SECRET) and m.get("chars") == len(SECRET) and m.get("sha256_12") == telemetry.text_facts(SECRET)["sha256_12"] and m.get("via_stdin") is False)
check("all four timings are recorded as numbers", all(isinstance(m.get(k), (int, float)) for k in ("connect_ms", "publish_ms", "ack_ms", "total_ms"))
      and sends[0]["payload"]["duration_ms"] == m["total_ms"])
check("PRIVACY (sender side): no part of the secret body is anywhere on disk", not any(part in every_byte_stored() for part in SECRET_PARTS))

fresh()
code, out, err = cli(["claude@other", "x"], CliNC({"status": "refused", "detail": "stop rule: more than 30 messages from a@b to c@d in 10 minutes"}))
m = meta(events("post_send")[0])
check("a refusal from an older relay (no reason in its answer) is classified from its sentence: pair limit, exit 4, blocked",
      code == 4 and m["result"] == "refused" and m["reason"] == "pair_limit" and m["limit"] == "pair" and events("post_send")[0]["payload"]["status"] == "blocked"
      and events("post_send")[0]["payload"]["error_message"] == "pair_limit")
fresh()
cli(["claude@other", "x"], CliNC({"status": "refused", "detail": "whatever wording", "reason": "recipient_limit"}))
check("a newer relay's own reason wins over the wording", meta(events("post_send")[0])["reason"] == "recipient_limit" and meta(events("post_send")[0])["limit"] == "recipient")

fresh()
code, out, err = cli(["claude@other", "x"], CliNC(answer=None))
m = meta(events("post_send")[0])
check("no answer from the relay: queued, exit 3, reply_timeout, reason relay_no_answer", code == 3 and m["result"] == "queued" and m["reply_timeout"] is True and m["reason"] == "relay_no_answer")
fresh()
code, out, err = cli(["codex@other", "x"], CliNC({"status": "queued", "detail": "nobody reachable", "reason": "nobody_there"}))
check("queued with a reason from the relay keeps it", code == 3 and meta(events("post_send")[0])["reason"] == "nobody_there" and meta(events("post_send")[0])["door"] == "codex")
fresh()
code, out, err = cli(["codex@other", "x"], CliNC({"status": "delivered", "detail": "delivered to codex@other (pid 5). Heads up: codex@other (pid 5) is mid-task (started 4 min ago) and reads this only when its task ends",
                                                  "targets": ["codex@other (pid 5)"]}))
check("a 'mid-task' heads-up becomes a busy note", meta(events("post_send")[0])["busy"] == ["mid_task"])
fresh()
cli(["grok@other", "x"], CliNC({"status": "delivered", "detail": "delivered to grok@other (pid 9). Heads up: grok@other (pid 9) cannot be woken: it reads this from its inbox", "targets": ["grok@other (pid 9)"]}))
check("a 'cannot be woken' heads-up becomes a busy note", meta(events("post_send")[0])["busy"] == ["cannot_wake"])

fresh()
code, out, err = cli(["claude@other", "x"], CliNC(fail_publish=OSError("secret-ish detail " + SECRET)))
m = meta(events("post_send")[0])
check("a publish failure: exit 2, reason publish_failed, only the exception CLASS recorded",
      code == 2 and m["result"] == "error" and m["reason"] == "publish_failed" and m["error_class"] == "OSError" and not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
code, out, err = cli(["claude@other", "x"], connect_error=lib.MailboxError("NoServersError()"))
m = meta(events("post_send")[0])
check("mailbox down: exit 2, reason mailbox_down, class no_servers, and no link event (it is this Mac's own mailbox)",
      code == 2 and m["reason"] == "mailbox_down" and m["error_class"] == "no_servers" and not events("post_link"))
fresh()
code, out, err = cli(["claude@other", "x"], connect_error=lib.MailboxError("Authorization Violation", refused=True))
check("this Mac's own key refused: reason key_refused", meta(events("post_send")[0])["reason"] == "key_refused")

fresh()
code, out, err = cli(["claude@proj@mac-b", "x"], connect_error=lib.MailboxError("cannot find mac-b on the network", setup=True))
m, links = meta(events("post_send")[0]), events("post_link")
check("another Mac that cannot be found: one post_link (setup, peer_dns) AND the post_send says cross_machine to that peer",
      code == 2 and len(links) == 1 and meta(links[0])["outcome"] == "setup" and meta(links[0])["peer"] == "mac-b" and meta(links[0])["error_class"] == "peer_dns"
      and m["cross_machine"] is True and m["peer"] == "mac-b" and m["reason"] == "peer_setup" and m["from_addr"] == "claude@myproj@mac-a" and m["to_addr"] == "claude@proj@mac-b")
fresh()
nc = CliNC()
code, out, err = cli(["claude@proj@mac-b", SECRET], nc, peer_error=lib.MailboxError("TimeoutError()"))
m = meta(events("post_send")[0])
check("another Mac asleep (timeout): outcome unreachable, class timeout (the link event is unchanged)", meta(events("post_link")[0])["outcome"] == "unreachable" and meta(events("post_link")[0])["error_class"] == "timeout")
check("another Mac asleep: the mail is stored here (forward subject) and the post_send says queued, peer_unreachable_stored, exit 3, warning",
      code == 3 and nc.published and nc.published[0][0] == "forward.mac-b.proj.claude" and m["result"] == "queued" and m["reason"] == "peer_unreachable_stored" and m["exit_code"] == 3
      and events("post_send")[0]["payload"]["status"] == "warning" and m["cross_machine"] is True and m["peer"] == "mac-b" and "publish_ms" in m)
check("PRIVACY (stored for forwarding): the secret body is nowhere in the event", not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
code, out, err = cli(["claude@proj@mac-b", "x"], CliNC(), connect_error=lib.MailboxError("TimeoutError()"))
check("another Mac asleep AND this Mac's own mailbox down: exit 2, reason mailbox_down (never a false queued)", code == 2 and meta(events("post_send")[0])["reason"] == "mailbox_down" and meta(events("post_send")[0])["result"] == "error")
fresh()
code, out, err = cli(["claude@proj@mac-b", "x"], connect_error=lib.MailboxError("Authorization Violation", refused=True))
check("another Mac rejecting this Mac's key: outcome refused", meta(events("post_link")[0])["outcome"] == "refused" and meta(events("post_send")[0])["reason"] == "peer_refused")
fresh()
code, out, err = cli(["claude@proj@mac-b", "x"], CliNC({"status": "delivered", "detail": "delivered to claude@proj (pid 31)", "targets": ["claude@proj (pid 31)"]}))
links = events("post_link")
check("a reachable other Mac: a post_link ok with the connect time, and the send is delivered cross-machine",
      code == 0 and len(links) == 1 and meta(links[0])["outcome"] == "ok" and isinstance(links[0]["payload"]["duration_ms"], float) and meta(events("post_send")[0])["result"] == "delivered")

fresh()
code, out, err = cli(["claude@other", "x"], sender=(None, ""))
check("cannot tell who is sending: exit 2, post_identity with the harness hint, and the send says why",
      code == 2 and meta(events("post_send")[0])["reason"] == "cannot_tell_sender" and len(events("post_identity")) == 1
      and isinstance(meta(events("post_identity")[0])["harness_hint"]["CLAUDECODE"], bool) and meta(events("post_identity")[0])["reason"] == "cannot_tell")
fresh()
code, out, err = cli(["--as", "bad name!!", "claude@other", "x"], sender=ValueError("not who-you-are"))
check("an invalid --as: post_identity bad_identity_option", code == 2 and meta(events("post_identity")[0])["reason"] == "bad_identity_option" and meta(events("post_identity")[0])["explicit_as"] is True)

fresh()
code, out, err = cli(["claude@other", "   "], CliNC({"status": "delivered", "detail": "x"}))
check("an empty message is reported and never reaches the network", code == 2 and meta(events("post_send")[0])["reason"] == "empty_message")
fresh()
nc = CliNC({"status": "delivered", "detail": "x"})
code, out, err = cli(["claude@other", "y" * (lib.MAX_TEXT_BYTES + 1)], nc)
m = meta(events("post_send")[0])
check("a message over the size cap: reason too_long, limit size, nothing published, only its size recorded",
      code == 2 and m["reason"] == "too_long" and m["limit"] == "size" and m["bytes"] == lib.MAX_TEXT_BYTES + 1 and nc.published == [])
fresh()
code, out, err = cli(["not an address@@@@", "x"], CliNC({"status": "delivered", "detail": "x"}))
check("a bad address: reason bad_address", meta(events("post_send")[0])["reason"] == "bad_address")
fresh()
code, out, err = cli(["claude@other", "-"], CliNC({"status": "delivered", "detail": "x", "targets": []}), stdin=SECRET + "\n")
check("a message read from standard input is flagged via_stdin and still not stored", meta(events("post_send")[0])["via_stdin"] is True and not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
with mock.patch.dict(os.environ, {"POSTBOX_CALLER": "inform-peers-release"}):
    cli(["claude@other", "x"], CliNC({"status": "delivered", "detail": "x"}))
check("a named caller (an automated sender) is recorded", meta(events("post_send")[0])["caller"] == "inform-peers-release")

fresh()
code, out, err = cli(["whoami"])
m = meta(events("post_whoami")[0])
check("post whoami: one post_whoami with the detection method", code == 0 and len(events()) == 1 and m["identity_method"] == "claude_env" and m["from_agent"] == "claude" and m["result"] == "ok")
fresh()
S = lib.Session
code, out, err = cli(["who"], sessions=[S("claude", 1, "/a/x", "x", "/s/1"), S("claude", 2, "/a/y", "y", "/s/2"), S("codex", 3, "/a/x", "x", "t")])
m = meta(events("post_who")[0])
check("post who (this Mac): sessions found, by agent, and how long discovery took", code == 0 and m["sessions_n"] == 3 and m["by_agent"] == {"claude": 2, "codex": 1} and isinstance(m["discover_ms"], float) and m["remote"] is False)


async def fake_remote_who(machine):
    return {"sessions": [{"agent": "claude", "project": "p", "pid": 1}]}
with mock.patch.object(post, "remote_who", fake_remote_who):
    fresh()
    code, out, err = cli(["who", "mac-b"])
m, links = meta(events("post_who")[0]), events("post_link")
check("post who <other Mac>: post_who (remote, peer, count) plus a post_link ok", code == 0 and m["remote"] is True and m["peer"] == "mac-b" and m["sessions_n"] == 1
      and len(links) == 1 and meta(links[0])["op"] == "who" and meta(links[0])["outcome"] == "ok")


async def broken_remote_who(machine):
    raise lib.MailboxError("TimeoutError()")
with mock.patch.object(post, "remote_who", broken_remote_who):
    fresh()
    code, out, err = cli(["who", "mac-b"])
check("post who <other Mac> that is unreachable: exit 2 and an unreachable post_link", code == 2 and meta(events("post_link")[0])["outcome"] == "unreachable" and meta(events("post_who")[0])["reason"] == "peer_unreachable")

fresh()
lib.inbox_put("claude", "myproj", SECRET + " one")
lib.inbox_put("claude", "myproj", "second")
before = len(events("post_inbox"))
code, out, err = cli(["inbox"])
take = [e for e in events("post_inbox") if meta(e).get("op") == "take"]
check("post inbox: one take event with the message count and bytes, and the text printed but not stored",
      code == 0 and len(take) == 1 and meta(take[0])["messages"] == 2 and meta(take[0])["bytes"] > 0 and SECRET in out and not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
code, out, err = cli(["inbox"])
check("post inbox with nothing waiting: op empty", meta(events("post_inbox")[0])["op"] == "empty" and meta(events("post_inbox")[0])["waiting"] == 0)
fresh()
with mock.patch.object(post, "watch_inbox", lambda agent, project: None):
    code, out, err = cli(["inbox", "--watch"])
check("post inbox --watch writes ONE event when it starts (a stopped process cannot report), not a second one at the end",
      len(events("post_inbox")) == 1 and meta(events("post_inbox")[0])["op"] == "watch")

# telemetry must not change what the command does
fresh()
nc1 = CliNC({"status": "delivered", "detail": "delivered to claude@other (pid 1)", "targets": ["claude@other (pid 1)"]})
with_tel = cli(["claude@other", "same words"], nc1)
with mock.patch.dict(os.environ, {"POSTBOX_TELEMETRY": "0"}):
    nc2 = CliNC({"status": "delivered", "detail": "delivered to claude@other (pid 1)", "targets": ["claude@other (pid 1)"]})
    without_tel = cli(["claude@other", "same words"], nc2)
check("with telemetry off the command prints the same and exits the same", with_tel == without_tel)
with mock.patch.object(post, "tel", lib._NoTelemetry()):
    nc3 = CliNC({"status": "delivered", "detail": "delivered to claude@other (pid 1)", "targets": ["claude@other (pid 1)"]})
    broken = cli(["claude@other", "same words"], nc3)
check("even with the telemetry module missing the command works", broken == with_tel)


# ================================================================= identity detection trace
print("-- identity trace")
def traced(env, **patches):
    lib.IDENTITY_TRACE.update(method="?", failed=["?"])
    with mock.patch.dict(os.environ, env, clear=True), contextlib.ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(lib, name, value))
        found = lib.my_identity()
    return found, dict(lib.IDENTITY_TRACE)

found, trace = traced({"GROK_AGENT": "1"}, grok_identity=lambda: ("grok", "p", 5))
check("identity trace: grok process", found == ("grok", "p", 5) and trace == {"method": "grok_process", "failed": []})
found, trace = traced({"CLAUDECODE": "1", "CLAUDE_PID": "123"}, cwd_of=lambda pid: "/a/myproj")
check("identity trace: claude env", found == ("claude", "myproj", 123) and trace["method"] == "claude_env")
found, trace = traced({"CLAUDECODE": "1", "CLAUDE_PID": "123"}, cwd_of=lambda pid: None, codex_ancestor=lambda: None, grok_identity=lambda: None)
check("identity trace: claude env present but its folder unreadable is a recorded failure", found is None and trace == {"method": "none", "failed": ["claude_env", "grok_process"]})
found, trace = traced({"CODEX_THREAD_ID": "not-a-uuid"})
check("identity trace: a bad codex thread id is a recorded failure", found is None and trace == {"method": "none", "failed": ["codex_thread"]})
found, trace = traced({}, codex_ancestor=lambda: types.SimpleNamespace(project="proj", pid=9, source="folder", where="", note=""), grok_identity=lambda: None)
check("identity trace: codex found from the process list", found == ("codex", "proj", 9) and trace["method"] == "codex_process")
found, trace = traced({}, codex_ancestor=lambda: None, grok_identity=lambda: None)
check("identity trace: nothing found", found is None and trace == {"method": "none", "failed": ["grok_process"]})
with mock.patch.dict(os.environ, {"POSTBOX_AS": "grok@myproj"}, clear=True), mock.patch.object(lib, "my_identity", lambda: None):
    me, how = lib.resolve_sender()
check("identity trace: POSTBOX_AS", me == ("grok", "myproj", None) and lib.IDENTITY_TRACE["method"] == "postbox_as")
with mock.patch.dict(os.environ, {}, clear=True):
    me, how = lib.resolve_sender("grok@myproj")
check("identity trace: --as", me == ("grok", "myproj", None) and lib.IDENTITY_TRACE["method"] == "flag_as")


# ================================================================= the relay
print("-- relay")
class Msg:
    def __init__(self, subject, text, headers, delivered=1, stored_ago=None, seq=None):
        self.subject, self.data, self.headers = subject, text.encode(), headers
        md = types.SimpleNamespace(num_delivered=delivered)
        if stored_ago is not None:
            md.timestamp = datetime.now(timezone.utc) - timedelta(seconds=stored_ago)
        if seq is not None:
            md.sequence = types.SimpleNamespace(stream=seq, consumer=seq)
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
    def __init__(self, targets, fail=None, note=""): self.targets, self.fail, self.note = targets, fail, note
    def resolve(self, project, exclude_pid=None): return self.targets
    def deliver(self, t, text):
        if self.fail:
            raise self.fail
    def busy_note(self, t): return self.note


HDR = {"From": "claude@myproj", "From-Pid": "1", "Reply-Inbox": "_INBOX.x"}
T1, T2 = lib.Session("claude", 11, "/a/p", "p", "/s/1"), lib.Session("claude", 12, "/a/p", "p", "/s/2")
run = asyncio.run


def handle(msg, door=None, doors=None, limiter=None, recipient_limiter=None):
    nc = NC()
    with contextlib.redirect_stdout(io.StringIO()):   # the relay prints one JSON log line per decision
        run(relay.handle(nc, msg, doors if doors is not None else {"claude": door}, limiter or lib.PairLimiter(), recipient_limiter or lib.PairLimiter(limit=100)))
    return nc


fresh()
nc = handle(Msg("post.p.claude", SECRET, HDR, stored_ago=2.5, seq=7), Door([T1, T2]))
ev = events("post_deliver")
m = meta(ev[0]) if ev else {}
check("a delivery writes one post_deliver: result, parties, door, sessions and pids",
      len(ev) == 1 and m["result"] == "delivered" and m["frm"] == "claude@myproj" and m["to"] == "claude@p" and m["door"] == "claude"
      and m["targets_n"] == 2 and m["target_pids"] == [11, 12] and m["failed_n"] == 0 and m["cross_machine"] is False and m["probe"] is False and ev[0]["payload"]["status"] == "success")
check("size, hash, attempts, queue age and the timings are recorded", m["bytes"] == len(SECRET) and m["sha256_12"] == telemetry.text_facts(SECRET)["sha256_12"] and m["attempts"] == 1
      and 2.0 <= m["queue_age_s"] <= 10 and all(isinstance(m[k], float) for k in ("resolve_ms", "deliver_ms")) and isinstance(ev[0]["payload"]["duration_ms"], float))
check("the sender's reply names a delivered status and carries no extra secrets", nc.sent[0][1]["status"] == "delivered")
check("PRIVACY (relay side): the secret body is nowhere on disk", not any(p in every_byte_stored() for p in SECRET_PARTS))
check("the delivery is counted for the hourly summary", relay.STATS.results[("delivered", "claude")] == 1)

fresh()
nc = handle(Msg("post.p.claude", "hi", HDR, seq=8), Door([]))
ev = events("post_deliver")
check("nobody there on the first try: written once as queued, and the sender is told with a reason",
      len(ev) == 1 and meta(ev[0])["result"] == "queued" and meta(ev[0])["reason"] == "nobody_there" and ev[0]["payload"]["status"] == "warning"
      and nc.sent[0][1]["reason"] == "nobody_there" and 8 in relay.QUEUED)
nc = handle(Msg("post.p.claude", "hi", HDR, delivered=3, seq=8), Door([]))
check("the 10-second retries of waiting mail are counted but NOT written (no flood)", len(events("post_deliver")) == 1 and relay.STATS.retry_naks == 1 and nc.sent == [])
nc = handle(Msg("post.p.claude", "hi", HDR, delivered=4, stored_ago=125, seq=8), Door([T1]))
last = events("post_deliver")[-1]
check("mail that waited and is then delivered: a delivered event with attempts and how long it waited, and it leaves the waiting list",
      meta(last)["result"] == "delivered" and meta(last)["attempts"] == 4 and 120 <= meta(last)["queue_age_s"] <= 135 and 8 not in relay.QUEUED
      and relay.STATS.delivered_after_queue == 1 and 120 <= relay.STATS.summary()["queue_wait_max_s"] <= 135)

fresh()
handle(Msg("post.p.claude", "a", HDR), Door([T1], fail=OSError("down")))
ev = events("post_deliver")
check("a door failure: result failed, reason door_error, the class of the error and no body", meta(ev[0])["result"] == "failed" and meta(ev[0])["reason"] == "door_error"
      and meta(ev[0])["fail_classes"] == ["OSError"] and ev[0]["payload"]["status"] == "error")
fresh()
nc = handle(Msg("post.p.claude", "a", HDR), Door([T1], fail=lib.InboxFull("inbox full")))
check("a full Grok inbox is its own reason, also told to the sender", meta(events("post_deliver")[0])["reason"] == "inbox_full" and meta(events("post_deliver")[0])["limit"] == "inbox"
      and nc.sent[0][1]["reason"] == "inbox_full")
fresh()
handle(Msg("post.p.claude", "a", HDR), Door([T1], note="codex@p (pid 5) is mid-task (started 3 min ago) and reads this only when its task ends"))
check("a mid-task note is recorded as a busy kind", meta(events("post_deliver")[0])["busy"] == ["mid_task"])

fresh()
limiter = lib.PairLimiter(limit=1)
handle(Msg("post.p.claude", "a", HDR), Door([T1]), limiter=limiter)
nc = handle(Msg("post.p.claude", "b", HDR), Door([T1]), limiter=limiter)
ev = events("post_deliver")[-1]
check("pair limit hit: refused, reason pair_limit, limit pair with its value; the reply carries the same reason",
      meta(ev)["result"] == "refused" and meta(ev)["reason"] == "pair_limit" and meta(ev)["limit"] == "pair" and meta(ev)["limit_value"] == 1 and ev["payload"]["status"] == "blocked"
      and nc.sent[0][1]["reason"] == "pair_limit")
fresh()
nc = handle(Msg("post.p.claude", "b", HDR), Door([T1]), recipient_limiter=lib.PairLimiter(limit=0))
check("recipient limit hit", meta(events("post_deliver")[0])["reason"] == "recipient_limit" and meta(events("post_deliver")[0])["limit"] == "recipient" and nc.sent[0][1]["reason"] == "recipient_limit")
fresh()
nc = handle(Msg("post.p.claude", "z" * (lib.MAX_TEXT_BYTES + 1), HDR), Door([T1]))
check("size cap hit: reason too_long, limit size, only the size recorded", meta(events("post_deliver")[0])["reason"] == "too_long" and meta(events("post_deliver")[0])["limit"] == "size"
      and meta(events("post_deliver")[0])["bytes"] == lib.MAX_TEXT_BYTES + 1 and nc.sent[0][1]["reason"] == "too_long")
fresh()
nc = handle(Msg("post.p.gemini", "b", HDR), doors={"claude": Door([T1])})
check("no delivery door: refused no_door", meta(events("post_deliver")[0])["reason"] == "no_door" and nc.sent[0][1]["reason"] == "no_door")
fresh()
handle(Msg("post.p.claude", "b", dict(HDR, **{"Reply-Inbox": "not-an-inbox"})), Door([T1]))
check("an invalid reply inbox is flagged on the event", meta(events("post_deliver")[0])["bad_reply_inbox"] is True and meta(events("post_deliver")[0])["result"] == "delivered")
fresh()
handle(Msg("post.p.claude", "b", dict(HDR, From="claude@proj@mac-b")), Door([T1]))
m = meta(events("post_deliver")[0])
check("mail from another Mac: cross_machine and the peer's name", m["cross_machine"] is True and m["peer"] == "mac-b" and m["frm"] == "claude@proj@mac-b" and relay.STATS.cross_in == 1)
fresh()
handle(Msg("garbage", "b", HDR), Door([T1]))
check("a malformed subject is refused bad_subject (and the subject itself is not recorded)", meta(events("post_deliver")[0])["reason"] == "bad_subject" and "garbage" not in every_byte_stored())
fresh()
handle(Msg("post.selfcheck.selfcheck", "self-check, please ignore", dict(HDR, From="installer@selfcheck@mac-b")), doors={"claude": Door([T1])})
m = meta(events("post_deliver")[0])
check("the health check's own test mail is flagged probe, counted apart and is not a refusal in the statistics",
      m["probe"] is True and m["cross_machine"] is True and events("post_deliver")[0]["payload"]["status"] == "success" and relay.STATS.probes == 1 and not relay.STATS.results)

# expiry
fresh()
relay.QUEUED[1] = {"stored": time.time() - lib.MAX_AGE_S - 600, "frm": "claude@a", "to": "claude@b", "door": "claude"}
relay.QUEUED[2] = {"stored": time.time() - 30, "frm": "claude@a", "to": "claude@b", "door": "claude"}
n = relay.sweep_expired_queue()
ev = events("post_expire")
check("mail that waited past the stream's age limit is reported once as expired; fresh mail is left alone",
      n == 1 and len(ev) == 1 and meta(ev[0])["kind"] == "queue" and meta(ev[0])["count"] == 1 and meta(ev[0])["oldest_age_s"] >= lib.MAX_AGE_S
      and meta(ev[0])["items"][0]["to"] == "claude@b" and list(relay.QUEUED) == [2] and relay.STATS.expired_queue == 1)
check("a second sweep reports nothing new", relay.sweep_expired_queue() == 0 and len(events("post_expire")) == 1)

# Grok inbox
fresh()
lib.inbox_put("grok", "myproj", SECRET)
put = events("post_inbox")
check("a Grok inbox put is recorded with its depth and size, not its text", len(put) == 1 and meta(put[0])["op"] == "put" and meta(put[0])["depth"] == 1 and meta(put[0])["bytes"] == len(SECRET)
      and not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
with mock.patch.object(lib, "INBOX_MAX_MESSAGES", 1):
    try:
        lib.inbox_put("grok", "myproj", "second")
    except lib.InboxFull:
        pass
full = [e for e in events("post_inbox") if meta(e)["op"] == "full"]
check("a full inbox is recorded (op full, depth, limit)", len(full) == 1 and meta(full[0])["depth"] == 1 and meta(full[0])["limit"] == 1 and full[0]["payload"]["status"] == "warning")
fresh()
old = time.time() - lib.MAX_AGE_S - 100
lib.inbox_put("grok", "oldproj", "stale message", now=old)
telemetry.reset(); shutil.rmtree(SPOOL, ignore_errors=True)
lib.inbox_list("grok", "oldproj")
ev = events("post_expire")
check("an unread Grok message that expired is recorded as post_expire kind inbox", len(ev) == 1 and meta(ev[0])["kind"] == "inbox" and meta(ev[0])["age_s"] >= lib.MAX_AGE_S)
check("inbox_depths counts unread mail without deleting anything", lib.inbox_depths().get("grok.myproj") == 1)

# stale mail and forwarded mail (the 7-day queue)
fresh()
handle(Msg("post.p.claude", SECRET, dict(HDR, **{"Sent-At": str(int(time.time()) - 7500)})), Door([T1]))
m = meta(events("post_deliver")[0])
check("stale mail delivered: post_deliver says stale and how long it waited (seconds), and the body is still not stored",
      m["stale"] is True and 7400 <= m["waited_s"] <= 7600 and m["result"] == "delivered" and not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
handle(Msg("post.p.claude", "hi", dict(HDR, **{"Sent-At": str(int(time.time()) - 30)})), Door([T1]))
check("fresh mail has no stale field", "stale" not in meta(events("post_deliver")[0]) and "waited_s" not in meta(events("post_deliver")[0]))


class PeerNC:
    def __init__(self, answer=None): self.answer, self.published = answer, []
    def new_inbox(self): return "_INBOX.pabc.1"
    async def subscribe(self, inbox): return self
    def jetstream(self): return self
    async def publish(self, subject, data, headers=None, timeout=None): self.published.append((subject, data, headers))
    async def next_msg(self, timeout=None):
        if self.answer is None:
            raise NatsTimeout()
        return types.SimpleNamespace(data=json.dumps(self.answer).encode())
    async def close(self): pass


def forward(msg, peer=None, connect_error=None, target_error=None):
    async def connect(target, timeout=3):
        if connect_error:
            raise connect_error
        return peer

    def target_of(name):
        if target_error:
            raise target_error
        return lib.Target("nats://peer", None, name)

    with contextlib.redirect_stdout(io.StringIO()):
        run(relay.handle_forward(msg, connect=connect, peer_target=target_of))


FWD_HDR = {"From": "claude@myproj@mac-a", "Nats-Msg-Id": "b" * 32, "Sent-At": str(int(time.time()) - 5000)}
fresh()
relay.PEER_DOWN.clear()
forward(Msg("forward.mac-b.proj.claude", SECRET, FWD_HDR, stored_ago=4000, seq=21), connect_error=lib.MailboxError("TimeoutError()"))
ev = events("post_forward")
m = meta(ev[0]) if ev else {}
check("mail kept for a sleeping Mac: one post_forward, forward_status stored, why, who, to which Mac, size, hash, how long it has waited",
      len(ev) == 1 and ev[0]["payload"]["component_name"] == "relay" and ev[0]["payload"]["status"] == "warning" and m["forward_status"] == "stored" and m["reason"] == "peer_unreachable"
      and m["frm"] == "claude@myproj@mac-a" and m["to"] == "claude@proj" and m["to_machine"] == "mac-b" and m["bytes"] == len(SECRET) and m["sha256_12"] == telemetry.text_facts(SECRET)["sha256_12"]
      and 3900 <= m["queue_age_s"] <= 4100 and m["attempts"] == 1 and isinstance(ev[0]["payload"]["duration_ms"], float))
forward(Msg("forward.mac-b.proj.claude", SECRET, FWD_HDR, delivered=5, stored_ago=4060, seq=21), connect_error=lib.MailboxError("TimeoutError()"))
check("the 60-second retries of waiting forward mail are counted, not written (no flood)", len(events("post_forward")) == 1 and relay.STATS.retry_naks == 1 and relay.STATS.forwards["stored"] == 1)
relay.PEER_DOWN.clear()
forward(Msg("forward.mac-b.proj.claude", SECRET, FWD_HDR, delivered=9, stored_ago=4500, seq=21), PeerNC({"status": "delivered", "detail": "delivered to claude@proj (pid 5)"}))
last = events("post_forward")[-1]
m = meta(last)
check("forwarded when the Mac answers: forward_status forwarded, the peer's own status, how many tries and how long it waited, success",
      m["forward_status"] == "forwarded" and m["peer_status"] == "delivered" and m["attempts"] == 9 and m["queue_age_s"] >= 4400 and last["payload"]["status"] == "success" and 21 not in relay.FWD_WAITING)
fresh()
forward(Msg("forward.mac-b.proj.claude", SECRET, FWD_HDR), PeerNC({"status": "queued", "detail": "nobody reachable", "reason": "nobody_there"}))
m = meta(events("post_forward")[0])
check("the other Mac stored it for nobody-there: forwarded, peer_status queued with its reason, warning", m["peer_status"] == "queued" and m["peer_reason"] == "nobody_there" and events("post_forward")[0]["payload"]["status"] == "warning")
fresh()
forward(Msg("forward.nowhere.proj.claude", SECRET, FWD_HDR), target_error=lib.MailboxError("no other Mac called 'nowhere' is linked to this one (linked: mac-b)", setup=True))
m = meta(events("post_forward")[0])
check("a Mac that is not linked: forward_status refused, reason unknown_peer, blocked", m["forward_status"] == "refused" and m["reason"] == "unknown_peer" and events("post_forward")[0]["payload"]["status"] == "blocked")
check("PRIVACY (forwarder): the secret body is nowhere on disk", not any(p in every_byte_stored() for p in SECRET_PARTS))
fresh()
relay.FWD_WAITING[1] = {"stored": time.time() - lib.MAX_AGE_S - 600, "frm": "claude@a@mac-a", "to": "codex@b", "machine": "mac-b"}
relay.FWD_WAITING[2] = {"stored": time.time() - 30, "frm": "claude@a@mac-a", "to": "codex@b", "machine": "mac-b"}
n = relay.sweep_expired_forwards()
ev = events("post_forward")
m = meta(ev[0]) if ev else {}
check("forward mail that outlived the stream's age limit: one post_forward expired with the count, the oldest age and who it was for; fresh mail untouched",
      n == 1 and len(ev) == 1 and m["forward_status"] == "expired" and m["count"] == 1 and m["oldest_age_s"] >= lib.MAX_AGE_S and m["items"][0]["machine"] == "mac-b"
      and m["by_machine"] == {"mac-b": 1} and list(relay.FWD_WAITING) == [2] and ev[0]["payload"]["status"] == "warning")
fresh()

# summary
class FakeJS:
    async def stream_info(self, name):
        return types.SimpleNamespace(state=types.SimpleNamespace(messages=3, first_seq=5))

    async def get_msg(self, stream, seq=None):
        return types.SimpleNamespace(time=datetime.now(timezone.utc) - timedelta(seconds=90), data=SECRET.encode(), seq=seq)

fresh()
handle(Msg("post.p.claude", "a", HDR), Door([T1]))
handle(Msg("post.p.claude", "a", HDR), Door([T1]))
handle(Msg("post.p.codex", "a", HDR, seq=3), doors={"codex": Door([])})
handle(Msg("post.p.codex", "a", HDR, delivered=2, seq=3), doors={"codex": Door([])})
handle(Msg("post.selfcheck.selfcheck", "x", HDR), doors={})
lib.inbox_put("grok", "sumproj", "w")
for _ in range(5):
    run(relay.maybe_summary(FakeJS()))
check("the summary is NOT written while the hour is not up (one per interval, never one per heartbeat)", not events("post_relay_summary"))
run(relay.maybe_summary(FakeJS(), force=True))
sm = events("post_relay_summary")
s = meta(sm[0]) if sm else {}
check("one hourly summary says what happened: counts by result and door, retries, probes, queue and inbox depth",
      len(sm) == 1 and s["delivered"] == 2 and s["queued"] == 1 and s["by_door"] == {"claude": {"delivered": 2}, "codex": {"queued": 1}} and s["retry_naks"] == 1 and s["probes"] == 1
      and s["queue_depth"] == 3 and 85 <= s["oldest_queued_age_s"] <= 100 and s["inbox_depth"] >= 1 and "window_s" in s and "uptime_s" in s)
check("the summary carries telemetry's own counters (silence vs broken telemetry)", s["telemetry"]["emitted"] >= 5 and set(s["telemetry"]) == {"emitted", "failed", "fallback", "disabled"})
check("the oldest waiting message's body is read for its time only: the secret in it is not stored", not any(p in every_byte_stored() for p in SECRET_PARTS))
check("after a summary the counters start again", relay.STATS.results == {} and relay.STATS.retry_naks == 0 and relay.STATS.probes == 0)
fresh()
with mock.patch.object(relay, "SUMMARY_EVERY_S", 0):
    run(relay.maybe_summary(FakeJS()))
check("when the interval is up the summary is written without being forced", len(events("post_relay_summary")) == 1)

# relay errors
fresh()
with contextlib.redirect_stdout(io.StringIO()):
    for _ in range(5):
        relay.log("relay_error", error="NoServersError('nats: no servers available " + SECRET + "')")
    relay.log("relay_error", error="Error('nats: Authorization Violation')")
    relay.log("heartbeat")
errs = events("post_relay_error")
classes = sorted(meta(e)["error_class"] for e in errs)
check("five repeats of one error class are written once; a different class is written too; heartbeats are not events",
      classes == ["NoServersError", "auth_violation"] and relay.STATS.errors["relay_error:NoServersError"] == 5 and relay.STATS.logs["heartbeat"] == 1 and len(events()) == 2)
check("the error text (which could hold anything) is never stored", not any(p in every_byte_stored() for p in SECRET_PARTS) and "no servers available" not in every_byte_stored())
with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(relay, "STATS", None):
    relay.log("relay_error", error="x")
check("a failure inside telemetry cannot break the relay's own log line", True)

# fallback file also stays clean
fresh()
shutil.rmtree(SUPPORT, ignore_errors=True)
os.makedirs(SUPPORT)
with mock.patch.object(telemetry, "spool_dir", lambda: None):
    handle(Msg("post.p.claude", SECRET, HDR), Door([T1]))
    cli(["claude@other", SECRET], CliNC({"status": "delivered", "detail": "x"}))
fb = Path(SUPPORT, "telemetry-fallback.ndjson")
check("PRIVACY (fallback file): events went to the fallback file and the secret is not in it",
      fb.is_file() and len(fb.read_text().splitlines()) >= 2 and not any(p in fb.read_text() for p in SECRET_PARTS))

# selfcheck
fresh()
async def fake_check_refused(peer=None):
    selfcheck.LAST["cls"] = "auth_refused"
    return 3
with mock.patch.object(selfcheck, "_run", fake_check_refused):
    run(selfcheck.run("mac-b"))
    run(selfcheck.run(None))
links = events("post_link")
check("selfcheck --peer writes one post_link (op selfcheck, refused); a check of this Mac's own mailbox writes none",
      len(links) == 1 and meta(links[0])["op"] == "selfcheck" and meta(links[0])["outcome"] == "refused" and meta(links[0])["peer"] == "mac-b" and meta(links[0])["error_class"] == "auth_refused")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
