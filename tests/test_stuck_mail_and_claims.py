#!/usr/bin/env python3
"""Offline recovery checks for durable Codex watches and public stuck notices.

The fixtures use only throwaway files and a fake notification executable.  They
never read a real conversation, process table, or local configuration.
"""
import asyncio
import json
import os
import sys
import tempfile

os.environ["POSTBOX_TELEMETRY"] = "0"
TMP = tempfile.mkdtemp(prefix="postbox-stuck-")
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(TMP, "watch.json")
os.environ["POSTBOX_HELD_DIR"] = os.path.join(TMP, "held")
os.environ["POSTBOX_SUPPORT_DIR"] = os.path.join(TMP, "support")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import lib  # noqa: E402
import relay  # noqa: E402

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("  ok  ", label)
    else:
        FAIL += 1
        print("  FAIL", label, detail)


S = lib.Session("codex", 22, "/public/project", "project", "thread-a", True,
                "folder", "", "", "work", "")
lib.codex_thread_rollout = lambda tid: os.path.join(TMP, tid + ".jsonl")
open(lib.codex_thread_rollout("thread-a"), "w").close()

print("-- durable watch states")
one = lib.codex_watch_reserve("message-opaque-1", S, "body stays local", "claude@project", "sender", now=100)
check("reservation is durable before delivery", one["state"] == "reserved" and one["id"] == "message-opaque-1", one)
again = lib.codex_watch_reserve("message-opaque-1", S, "different body", "claude@project", "sender", now=101)
check("duplicate reservation keeps the first stable record", again == one and len(lib.codex_watch_load()) == 1, lib.codex_watch_load())
check("queue acceptance changes the same record", lib.codex_watch_mark_queued("message-opaque-1", now=102) and lib.codex_watch_load()[0]["state"] == "queued", lib.codex_watch_load())

print("-- interrupted turn gets one recovery opportunity")
open(lib.codex_thread_rollout("thread-a"), "a").write('{"type":"event_msg","payload":{"type":"turn_aborted"}}\n')
lib.codex_busy_seconds = lambda _thread, now=None: None
first = lib.codex_watch_check(now=100 + lib.CODEX_SEEN_AFTER_S + 1)
check("an aborted queued turn requests one retry", [kind for _entry, kind in first] == ["retry"], first)
second = lib.codex_watch_check(now=101 + lib.CODEX_SEEN_AFTER_S + 1)
check("the retry is bounded and then settles unseen", [kind for _entry, kind in second] == ["unseen"], second)

print("-- notification payload is metadata-only")
capture = os.path.join(TMP, "notice.json")
hook = os.path.join(TMP, "hook.py")
open(hook, "w").write("import json,sys; open(sys.argv[1], 'w').write(sys.stdin.read())\n")
relay.STUCK_NOTIFY_CMD = [sys.executable, hook, capture]
payload = relay.stuck_notification("message-opaque-1", "queued", "claude@project", "codex@project", 601, "claim_not_ready", 600)
check("payload has exactly the portable public fields", set(payload) == {"id", "kind", "frm", "to", "age_s", "reason", "limit_s"}, payload)
check("notification writes only the JSON payload", json.loads(open(capture).read()) == payload and "body stays local" not in open(capture).read(), open(capture).read())
relay.STUCK_NOTIFY_CMD = []

print("-- one exact safe reroute")
S2 = lib.Session("codex", 23, "/public/project", "project", "thread-b", True,
                 "folder", "", "", "replacement", "")
open(lib.codex_thread_rollout("thread-b"), "w").close()
lib.codex_watch_reserve("message-opaque-2", S, "recovery body", "claude@project", now=200)
lib.codex_watch_mark_queued("message-opaque-2", now=201)
entry = lib.codex_watch_get("message-opaque-2")


class Door(lib.CodexDoor):
    def __init__(self): self.calls = []
    def candidates(self, project, exclude_pid=None):
        return lib.CodexCandidates([S2], [])
    def deliver(self, session, body, timeout=25.0):
        self.calls.append((session.target, body))


door = Door()
original_check = lib.codex_watch_check
lib.codex_watch_check = lambda: [(entry, "retry")]
asyncio.run(relay.check_codex_seen(None, {"codex": door}))
lib.codex_watch_check = original_check
row = lib.codex_watch_get("message-opaque-2")
check("one fresh exact target receives the recovery", door.calls == [("thread-b", "recovery body")], door.calls)
check("reroute is persisted and re-armed without a duplicate", row["thread"] == "thread-b" and row["reroute_count"] == 1 and row["state"] == "queued", row)

print("-- unresolved sibling prevents reroute")
W = lib.Session("codex", 24, "/public/project", "project", "", False,
                "folder", "", "", "waiting", "")
lib.codex_watch_reserve("message-opaque-3", S, "must not duplicate", "claude@project", now=300)
lib.codex_watch_mark_queued("message-opaque-3", now=301)
blocked = lib.codex_watch_get("message-opaque-3")


class AmbiguousDoor(Door):
    def candidates(self, project, exclude_pid=None):
        return lib.CodexCandidates([S2], [W])


ambiguous = AmbiguousDoor()
lib.codex_watch_check = lambda: [(blocked, "retry")]
asyncio.run(relay.check_codex_seen(None, {"codex": ambiguous}))
lib.codex_watch_check = original_check
check("waiting sibling blocks a reroute and duplicate", ambiguous.calls == [], ambiguous.calls)

print("-- busy delivery waits before recovery")
lib.codex_watch_reserve("message-opaque-busy", S, "busy body", "claude@project", now=400)
lib.codex_watch_mark_queued("message-opaque-busy", now=401)
open(lib.codex_thread_rollout("thread-a"), "a").write('{"type":"event_msg","payload":{"type":"turn_aborted"}}\n')
lib.codex_busy_seconds = lambda _thread, now=None: 90
check("busy conversation does not trigger an early resend", lib.codex_watch_check(now=400 + lib.CODEX_SEEN_AFTER_S + 1) == [], "busy watch settled early")
lib.codex_busy_seconds = lambda _thread, now=None: None
busy_out = lib.codex_watch_check(now=400 + lib.CODEX_SEEN_AFTER_S + 2)
check("idle aborted conversation gets one later retry", any(e.get("id") == "message-opaque-busy" and kind == "retry" for e, kind in busy_out), busy_out)

print("-- held diagnostics repeat at the threshold without text")
hold_id = lib.hold_put({"msg_id": "held-stuck-id", "agent": "codex", "project": "project", "frm": "claude@project",
                        "session": "", "pid": 1, "text": "private held body", "sent_at": 100}, now=100)
due = lib.hold_stuck_due(600, now=701)
check("old held mail produces one metadata-only diagnostic", len(due) == 1 and due[0]["id"] == hold_id and "text" not in due[0], due)
check("the same interval does not repeat the alarm", lib.hold_stuck_due(600, now=800) == [], "repeated too soon")
check("a later threshold interval alarms again", len(lib.hold_stuck_due(600, now=1302)) == 1, "not periodic")

print("-- relay path avoids duplicate queue calls")


class Msg:
    def __init__(self, delivered):
        self.subject, self.data = "post.project.codex", b"one queued payload"
        self.headers = {"From": "claude@project", "From-Pid": "1", "Nats-Msg-Id": "stable-stream-id"}
        self.metadata = type("Meta", (), {"num_delivered": delivered, "sequence": type("Seq", (), {"stream": 88})()})()
        self.acked = self.naked = None
    async def ack(self): self.acked = True
    async def nak(self, delay=None): self.naked = delay
    async def term(self): raise AssertionError("unexpected refusal")


class NC:
    def __init__(self): self.answers = []
    async def publish(self, subject, data): self.answers.append((subject, json.loads(data)))
    async def flush(self, timeout=None): pass


class FixedDoor(Door):
    def candidates(self, project, exclude_pid=None):
        return lib.CodexCandidates([S2], [])


fixed = FixedDoor()
first_msg, second_msg, nc = Msg(1), Msg(2), NC()
asyncio.run(relay.handle(nc, first_msg, {"codex": fixed}, lib.PairLimiter(), lib.PairLimiter(limit=100)))
asyncio.run(relay.handle(nc, second_msg, {"codex": fixed}, lib.PairLimiter(), lib.PairLimiter(limit=100)))
check("redelivery after queue acceptance is acknowledged without a second queue call", first_msg.acked and second_msg.acked and len(fixed.calls) == 1, fixed.calls)

print("-- relay emits the held threshold payload")
relay.STUCK_NOTIFY_CMD = [sys.executable, hook, capture]
out = asyncio.run(relay.check_stuck_holds(now=1903))
payload = json.loads(open(capture).read())
check("periodic held hook has only the public contract", out and payload == out[0] and set(payload) == {"id", "kind", "frm", "to", "age_s", "reason", "limit_s"} and "private held body" not in open(capture).read(), payload)
relay.STUCK_NOTIFY_CMD = []

print("-- queued stuck alarms have durable cadence")
check("thread-not-ready is due once at its threshold", lib.stuck_notice_due("queued-thread", "queued", 601, "thread_not_ready", 600, now=1000), "not due")
check("thread-not-ready does not repeat during the same interval", not lib.stuck_notice_due("queued-thread", "queued", 602, "thread_not_ready", 600, now=1100), "repeated")
check("claim-not-ready gets an independent threshold alarm", lib.stuck_notice_due("queued-claim", "queued", 601, "claim_not_ready", 600, now=1000), "not due")
check("the next persisted interval allows one new queued alarm", lib.stuck_notice_due("queued-thread", "queued", 1201, "thread_not_ready", 600, now=1601), "not due later")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
