#!/usr/bin/env python3
"""Synthetic checks for relay-backed local session listings."""
import asyncio
import json
import os
import sys
import tempfile
import uuid

os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["POSTBOX_MACHINE"] = "mac-a"
os.environ["POSTBOX_SUPPORT_DIR"] = tempfile.mkdtemp(prefix="postbox-who-")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import lib  # noqa: E402
import post  # noqa: E402
import relay  # noqa: E402

PASS = FAIL = 0
THREAD = str(uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL", label, detail)


async def exercise_relay_answer():
    session = lib.Session("codex", 23, "/synthetic/project-a", "project-a", THREAD,
                          True, "marker", "", "synthetic note", "work", "123abc")
    object.__setattr__(session, "claimed", True)
    object.__setattr__(session, "delivery_issue", "last_delivery_not_seen")
    rows = []

    class Connection:
        async def publish(self, _subject, data):
            rows.append(json.loads(data))
        async def flush(self, timeout=2):
            return None

    class Message:
        reply = "_INBOX.synthetic"

    before = lib.all_sessions
    lib.all_sessions = lambda: [session]
    try:
        await relay.answer_who(Connection(), Message())
    finally:
        lib.all_sessions = before
    return rows[0]["sessions"][0]


row = asyncio.run(exercise_relay_answer())
check("relay includes folder, source and note", row.get("folder") == "project-a"
      and row.get("source") == "marker" and row.get("note") == "synthetic note", row)
check("relay answer carries a claim only as an exact thread id", row.get("claimed") is True
      and row.get("thread") == THREAD, row)
check("relay answer contains no absolute path", not any("/" in str(value) for value in row.values()), row)
check("relay carries the last-delivery health state", row.get("delivery_issue") == "last_delivery_not_seen", row)

raw = {"machine": "mac-a", "sessions": [row], "inbox_only": []}
converted = post.sessions_from_relay(raw)
check("relay conversion keeps only public session metadata", len(converted) == 1
      and converted[0].project == "project-a" and converted[0].target == THREAD
      and converted[0].claimed and converted[0].source == "marker", converted)
check("conversion never adopts a path from the relay", converted[0].cwd.endswith("project-a")
      and converted[0].cwd != "/synthetic/project-a", converted[0].cwd)
check("an older relay answer remains usable", len(post.sessions_from_relay({"sessions": [
    {"agent": "claude", "project": "project-a", "pid": 42, "ready": True}
]})) == 1, "legacy response rejected")
check("malformed relay data requests local fallback", post.sessions_from_relay({"sessions": "bad"}) is None,
      "malformed response accepted")
check("unusable relay rows request local fallback", post.sessions_from_relay({"sessions": ["bad"]}) is None,
      "unusable rows accepted")
unsafe_note = post.sessions_from_relay({"sessions": [
    {"agent": "codex", "project": "project-a", "pid": 43, "ready": True, "note": "ignored /synthetic/private/path"}
]})
check("relay notes containing a path are removed", unsafe_note is not None and len(unsafe_note) == 1
      and unsafe_note[0].note == "", unsafe_note)

local_calls = []
async def fake_local_relay_who():
    return raw

before_relay = post.local_relay_who
before_all = lib.all_sessions
before_identity, before_trace = lib.my_identity, lib.IDENTITY_TRACE
post.local_relay_who = fake_local_relay_who
lib.all_sessions = lambda: local_calls.append(True) or []
lib.my_identity = lambda: ("claude", "project-a", 77)
lib.IDENTITY_TRACE = {"method": "synthetic"}
try:
    from contextlib import redirect_stdout
    from io import StringIO
    output = StringIO()
    with redirect_stdout(output):
        post.cmd_who([])
finally:
    post.local_relay_who = before_relay
    lib.all_sessions = before_all
    lib.my_identity, lib.IDENTITY_TRACE = before_identity, before_trace
check("local post who uses the relay response", "codex@project-a" in output.getvalue()
      and not local_calls, output.getvalue())
check("local post who displays the relay's health metadata", "last delivery not seen" in output.getvalue(),
      output.getvalue())
check("a Claude caller does not claim every Codex context row", "<- this session" not in output.getvalue(),
      output.getvalue())

before_thread = os.environ.get("CODEX_THREAD_ID")
other_thread = str(uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))
other_claim = dict(row, pid=25, thread=other_thread, folder="project-a", delivery_issue="")
codex_raw = {"machine": "mac-a", "sessions": [row, other_claim], "inbox_only": []}
async def fake_two_claims():
    return codex_raw

post.local_relay_who = fake_two_claims
lib.my_identity = lambda: None
os.environ["CODEX_THREAD_ID"] = THREAD
try:
    output = StringIO()
    with redirect_stdout(output):
        post.cmd_who([])
finally:
    post.local_relay_who = before_relay
    lib.all_sessions = before_all
    lib.my_identity, lib.IDENTITY_TRACE = before_identity, before_trace
    if before_thread is None:
        os.environ.pop("CODEX_THREAD_ID", None)
    else:
        os.environ["CODEX_THREAD_ID"] = before_thread
check("a Codex caller marks only its exact claimed thread", output.getvalue().count("<- this session") == 1,
      output.getvalue())

async def silent_relay():
    raise TimeoutError("synthetic relay silence")

before_relay = post.local_relay_who
before_all = lib.all_sessions
before_identity, before_trace = lib.my_identity, lib.IDENTITY_TRACE
post.local_relay_who = silent_relay
lib.all_sessions = lambda: []
lib.my_identity = lambda: None
lib.IDENTITY_TRACE = {"method": "synthetic"}
try:
    output = StringIO()
    with redirect_stdout(output):
        post.cmd_who([])
finally:
    post.local_relay_who = before_relay
    lib.all_sessions = before_all
    lib.my_identity, lib.IDENTITY_TRACE = before_identity, before_trace
check("silent relay falls back with a visible note", "local discovery" in output.getvalue().lower(),
      output.getvalue())

async def unusable_relay():
    return {"sessions": ["bad"], "inbox_only": []}

before_relay = post.local_relay_who
before_all = lib.all_sessions
before_identity, before_trace = lib.my_identity, lib.IDENTITY_TRACE
post.local_relay_who = unusable_relay
lib.all_sessions = lambda: [lib.Session("claude", 78, "/synthetic/project-a", "project-a", "",
                                         True, "folder", "", "", "", "")]
lib.my_identity = lambda: None
lib.IDENTITY_TRACE = {"method": "synthetic"}
try:
    output = StringIO()
    with redirect_stdout(output):
        post.cmd_who([])
finally:
    post.local_relay_who = before_relay
    lib.all_sessions = before_all
    lib.my_identity, lib.IDENTITY_TRACE = before_identity, before_trace
check("returned unusable relay data falls back and says why",
      "returned unusable data" in output.getvalue() and "claude@project-a" in output.getvalue(), output.getvalue())

waiting_claim_pid = lib.Session("codex", 33, "project-a", "project-a", "", False)
waiting_sibling = lib.Session("codex", 34, "project-a", "project-a", "", False)
claimed = lib.Session("codex", 33, "project-a", "project-a", THREAD, True, claimed=True)
old_claude, old_snapshot, old_claims = lib.claude_sessions, lib.codex_discovery_snapshot, lib.thread_context_claims
old_enabled = lib.doors.enabled
old_identity, old_trace = lib.my_identity, lib.IDENTITY_TRACE
lib.claude_sessions = lambda: []
lib.codex_discovery_snapshot = lambda: ([waiting_claim_pid, waiting_sibling], [{"pid": 33}, {"pid": 34}])
lib.thread_context_claims = lambda **_kwargs: [claimed]
lib.doors.enabled = lambda agent: agent == "codex"
lib.my_identity = lambda: None
lib.IDENTITY_TRACE = {"method": "synthetic"}
try:
    listed = lib.all_sessions()
finally:
    lib.claude_sessions, lib.codex_discovery_snapshot, lib.thread_context_claims = old_claude, old_snapshot, old_claims
    lib.doors.enabled = old_enabled
    lib.my_identity, lib.IDENTITY_TRACE = old_identity, old_trace
check("an exact claim replaces only its same-PID unresolved row",
      [(s.pid, s.target) for s in listed] == [(34, ""), (33, THREAD)], listed)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
