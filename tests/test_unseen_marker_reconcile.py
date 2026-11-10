#!/usr/bin/env python3
"""Synthetic checks for delivery-health recovery evidence."""
import json
import os
import sys
import tempfile
import uuid
import asyncio

os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["POSTBOX_SUPPORT_DIR"] = tempfile.mkdtemp(prefix="postbox-health-")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import lib  # noqa: E402
import relay  # noqa: E402

_ORIGINAL_CLAIMS = lib.thread_context_claims
lib.thread_context_claims = lambda *_args, **_kwargs: []

PASS = FAIL = 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        print("FAIL", label, detail)


support = lib.support_dir()
health = support / "delivery-health"
health.mkdir(parents=True, exist_ok=True)
thread = str(uuid.uuid4())
delivery_id = "d" * 32
marker = health / f"{thread}.json"
check_recorded = lib.record_unseen_delivery(thread, delivery_id)
marker.write_text(json.dumps({"thread": thread, "delivery_id": delivery_id, "state": "unseen"}))
os.utime(marker, (100, 100))
rollout = support / "conversation.jsonl"

relay_thread = str(uuid.uuid4())
watch = {"id": "synthetic-message", "thread": relay_thread, "sent": 1000,
         "to": "agent-a@project-a", "frm": "agent-b@project-a"}
old_watch_check, old_notify_sender = lib.codex_watch_check, relay.notify_sender
old_notices = relay.STUCK_NOTIFY_CMD
lib.codex_watch_check = lambda: [(watch, "unseen")]
relay.STUCK_NOTIFY_CMD = []

async def sent_notice(*_args, **_kwargs):
    return "sent"

relay.notify_sender = sent_notice
try:
    asyncio.run(relay.check_codex_seen(None))
finally:
    lib.codex_watch_check, relay.notify_sender = old_watch_check, old_notify_sender
    relay.STUCK_NOTIFY_CMD = old_notices
check("relay records a metadata-only unseen marker", lib.read_delivery_health(relay_thread) == "last_delivery_not_seen",
      lib.read_delivery_health(relay_thread))


def write_record(role, text):
    record = {"type": "response_item", "payload": {
        "type": "message", "role": role,
        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
    }}
    rollout.write_text(json.dumps(record) + "\n")


lib._HEALTH_LAST_TRY.clear()
check("unseen marker stores only exact metadata", check_recorded and
      set(json.loads(marker.read_text())) == {"thread", "delivery_id", "state"}, marker.read_text())
session = lib.Session("codex", 23, "project-a", "project-a", thread, True)
annotated = lib.annotate_delivery_health(session)
check("unseen marker makes its exact thread visibly unavailable",
      not annotated.ready and annotated.delivery_issue == "last_delivery_not_seen", annotated)
candidates = lib.CodexDoor().candidates("project-a", sessions=[session], screen_snapshot=[])
check("an unseen thread is held until the exact conversation has recovery evidence",
      candidates.ready == [] and len(candidates.waiting) == 1
      and candidates.waiting[0].delivery_issue == "last_delivery_not_seen", candidates)
sender_thread, sender_delivery = str(uuid.uuid4()), "7" * 32
sender_marker = health / f"{sender_thread}.json"
sender_marker.write_text(json.dumps({"thread": sender_thread, "delivery_id": sender_delivery, "state": "unseen"}))
sender_session = lib.Session("codex", 23, "project-a", "project-a", sender_thread, True)
ready_peer = lib.Session("codex", 24, "project-a", "project-a", str(uuid.uuid4()), True)
before_claims = lib.thread_context_claims
lib.thread_context_claims = lambda *_args, **_kwargs: [sender_session]
try:
    sender_excluded = lib.CodexDoor().candidates("project-a", 23, sessions=[sender_session, ready_peer], screen_snapshot=[])
    try:
        thread_excluded = lib.CodexDoor().candidates("project-a", None, sessions=[sender_session, ready_peer],
                                                      screen_snapshot=[], exclude_thread=sender_thread)
    except TypeError:
        thread_excluded = None
finally:
    lib.thread_context_claims = before_claims
check("the sender's flagged claim is excluded by sender PID", sender_excluded.waiting == []
      and [s.target for s in sender_excluded.ready] == [ready_peer.target], sender_excluded)
check("the sender's flagged claim is excluded by exact thread", thread_excluded is not None
      and thread_excluded.waiting == [] and [s.target for s in thread_excluded.ready] == [ready_peer.target], thread_excluded)
ready_own_thread = str(uuid.uuid4())
ready_claim = lib.Session("codex", 25, "project-a", "project-a", ready_own_thread, True, claimed=True)
before_claims = lib.thread_context_claims
lib.thread_context_claims = lambda *_args, **_kwargs: [ready_claim]
try:
    ready_candidates = lib.CodexDoor().candidates("project-a", sessions=[ready_peer], screen_snapshot=[],
                                                   exclude_thread=ready_own_thread)
    ready_resolved = lib.CodexDoor().resolve("project-a", sessions=[ready_claim], exclude_thread=ready_own_thread)
finally:
    lib.thread_context_claims = before_claims
check("candidates excludes a ready claim for the sender's exact thread",
      [s.target for s in ready_candidates.ready] == [ready_peer.target], ready_candidates)
check("resolve excludes a ready claim for the sender's exact thread",
      ready_resolved == [], ready_resolved)
check("resolve excludes a thread with a delivery-health flag",
      lib.CodexDoor().resolve("project-a", sessions=[session]) == [], "flagged thread remained routable")
write_record("assistant", f"Postbox delivery reference: {delivery_id}")
os.utime(rollout, (120, 120))
cleared = lib.reconcile_delivery_health(directory=health, rollout_of=lambda _thread: rollout, now=1000)
check("receipt-like assistant text alone does not clear the marker", not cleared and marker.exists(), cleared)

write_record("user", f"{delivery_id}")
os.utime(rollout, (120, 120))
lib._HEALTH_LAST_TRY.clear()
cleared = lib.reconcile_delivery_health(directory=health, rollout_of=lambda _thread: rollout, now=2000)
check("a bare identifier does not clear the marker", not cleared and marker.exists(), cleared)

lib._HEALTH_LAST_TRY.clear()
cleared = lib.reconcile_delivery_health(directory=health, rollout_of=lambda _thread: rollout, now=3000)
check("the bare delivery id does not clear without claim or log-write proof", not cleared and marker.exists(), cleared)

reference_thread, reference_id = str(uuid.uuid4()), "9" * 32
reference_marker = health / f"{reference_thread}.json"
reference_marker.write_text(json.dumps({"thread": reference_thread, "delivery_id": reference_id, "state": "unseen"}))
reference_log = support / "reference.jsonl"
reference_log.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": f"Postbox delivery reference: {reference_id}"}]}}) + "\n")
lib._HEALTH_LAST_TRY.clear()
cleared = lib.reconcile_delivery_health(directory=health,
                                        rollout_of=lambda tid: reference_log if tid == reference_thread else None, now=3500)
check("receipt-like user text does not clear without claim or log-write proof",
      not cleared and reference_marker.exists(), cleared)

os.utime(rollout, (marker.stat().st_mtime + 30, marker.stat().st_mtime + 30))
lib._HEALTH_LAST_TRY.clear()
check("a log write exactly 30 seconds later does not clear the marker",
      lib.reconcile_delivery_health(directory=health, rollout_of=lambda _thread: rollout, now=4000) == []
      and marker.exists(), marker.exists())
write_record("assistant", f"Postbox delivery reference: {delivery_id}")
os.utime(rollout, (marker.stat().st_mtime + 31, marker.stat().st_mtime + 31))
lib._HEALTH_LAST_TRY.clear()
cleared = lib.reconcile_delivery_health(directory=health, rollout_of=lambda _thread: rollout, now=5000)
check("the conversation log clears the marker after a write more than 30 seconds later",
      cleared == [thread] and not marker.exists(), cleared)
check("cleared thread no longer carries the warning",
      lib.annotate_delivery_health(session).delivery_issue == "", lib.annotate_delivery_health(session))

claimed_thread, claimed_id = str(uuid.uuid4()), "6" * 32
claim_health = health / "claim-proof"
claim_health.mkdir()
claimed_marker = claim_health / f"{claimed_thread}.json"
claimed_marker.write_text(json.dumps({"thread": claimed_thread, "delivery_id": claimed_id, "state": "unseen"}))
claim_path = lib._codex_claim_path(claimed_thread)
claim_path.parent.mkdir(parents=True, exist_ok=True)
claim_path.write_text("synthetic claim timestamp")
os.utime(claimed_marker, (100, 100))
os.utime(claim_path, (101, 101))
before_claims = lib.thread_context_claims
lib.thread_context_claims = lambda *_args, **_kwargs: [lib.Session("codex", 24, "project-a", "project-a", claimed_thread, True)]
try:
    cleared = lib.reconcile_delivery_health(directory=claim_health, rollout_of=lambda _thread: None, now=5500)
finally:
    lib.thread_context_claims = before_claims
check("a context claim written after the verdict clears its marker",
      cleared == [claimed_thread] and not claimed_marker.exists(), cleared)

calls = []
async def no_op(*_args, **_kwargs):
    return []
old_seen, old_holds, old_summary, old_reconcile = (relay.check_codex_seen, relay.check_stuck_holds,
                                                       relay.maybe_summary, lib.reconcile_delivery_health)
relay.check_codex_seen = no_op
relay.check_stuck_holds = no_op
relay.maybe_summary = no_op
lib.reconcile_delivery_health = lambda: calls.append("reconciled") or []
try:
    asyncio.run(relay.quiet_round(None, None, 1))
finally:
    relay.check_codex_seen, relay.check_stuck_holds = old_seen, old_holds
    relay.maybe_summary, lib.reconcile_delivery_health = old_summary, old_reconcile
check("relay quiet round runs reconciliation", calls == ["reconciled"], calls)

extra = health / "additional"
extra.mkdir()

def marker_for(thread_id, reference, state="unseen"):
    path = extra / f"{thread_id}.json"
    path.write_text(json.dumps({"thread": thread_id, "delivery_id": reference, "state": state}))
    return path


unrelated_thread, unrelated_id = str(uuid.uuid4()), "e" * 32
unrelated_marker = marker_for(unrelated_thread, unrelated_id)
unrelated_log = support / "unrelated.jsonl"
write = {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "ordinary follow-up"}]}}
unrelated_log.write_text(json.dumps(write) + "\n")
check("a missing exact reference keeps the flag", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: unrelated_log, now=4000) == [] and unrelated_marker.exists(), unrelated_marker.exists())

other_thread, other_id = str(uuid.uuid4()), "f" * 32
other_marker = marker_for(other_thread, other_id)
write_record("user", f"Postbox delivery reference: {delivery_id}")
lib._HEALTH_LAST_TRY.clear()
check("a different delivery reference does not clear this flag", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: rollout, now=5000) == [] and other_marker.exists(), other_marker.exists())

missing_thread, missing_id = str(uuid.uuid4()), "1" * 32
missing_marker = marker_for(missing_thread, missing_id)
lib._HEALTH_LAST_TRY.clear()
check("a missing conversation log keeps the flag", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: None, now=6000) == [] and missing_marker.exists(), missing_marker.exists())
bad_thread = str(uuid.uuid4())
bad_marker = extra / f"{bad_thread}.json"
bad_marker.write_text("not-json")
check("an unreadable marker is left alone", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: rollout, now=7000) == [] and bad_marker.exists(), bad_marker.exists())

symlink_thread = str(uuid.uuid4())
symlink_target = extra / "symlink-target.json"
symlink_target.write_text(json.dumps({"thread": symlink_thread, "delivery_id": "8" * 32, "state": "unseen"}))
symlink_marker = extra / f"{symlink_thread}.json"
symlink_marker.symlink_to(symlink_target)
check("a symlinked marker is ignored", lib.read_delivery_health(symlink_thread) == "", symlink_marker)

queued_thread, queued_id = str(uuid.uuid4()), "2" * 32
queued_marker = marker_for(queued_thread, queued_id, state="queued")
check("a non-unseen state is not reconciled", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: rollout, now=8000) == [] and queued_marker.exists(), queued_marker.exists())

window_thread, window_id = str(uuid.uuid4()), "3" * 32
window_marker = marker_for(window_thread, window_id)
window_log = support / "window.jsonl"
window_log.write_text(json.dumps(write) + "\n")
os.utime(window_marker, (100, 100))
os.utime(window_log, (120, 120))
lib._HEALTH_LAST_TRY.clear()
check("a recent conversation log leaves the flag", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: window_log, now=9000) == [] and window_marker.exists(), window_marker.exists())
window_log.write_text(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": f"Postbox delivery reference: {window_id}"}]}}) + "\n")
os.utime(window_log, (130, 130))
check("the same retry window does not rescan the log", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: window_log, now=9001) == [] and window_marker.exists(), window_marker.exists())
os.utime(window_log, (131, 131))
check("the later retry window clears the now-visible reference", lib.reconcile_delivery_health(
    directory=extra, rollout_of=lambda _thread: window_log, now=9031) == [window_thread], window_thread)

race_thread, old_id, new_id = str(uuid.uuid4()), "4" * 32, "5" * 32
race_dir = health / "race"
race_dir.mkdir()
race_marker = race_dir / f"{race_thread}.json"
race_marker.write_text(json.dumps({"thread": race_thread, "delivery_id": old_id, "state": "unseen"}))
old_health_dir = lib._delivery_health_dir
race_claim_path = lib._codex_claim_path(race_thread)
race_claim_path.parent.mkdir(parents=True, exist_ok=True)
race_claim_path.write_text("synthetic race claim")
os.utime(race_marker, (200, 200))
os.utime(race_claim_path, (201, 201))
old_claims = lib.thread_context_claims
def replace_during_scan(*_args, **_kwargs):
    lib._delivery_health_dir = lambda: race_dir
    lib.record_unseen_delivery(race_thread, new_id)
    return [lib.Session("codex", 24, "project-a", "project-a", race_thread, True)]
lib._HEALTH_LAST_TRY.clear()
lib.thread_context_claims = replace_during_scan
try:
    race_result = lib.reconcile_delivery_health(directory=race_dir, rollout_of=lambda _thread: rollout, now=10000)
finally:
    lib.thread_context_claims = old_claims
    lib._delivery_health_dir = old_health_dir
check("a newer marker survives concurrent claim reconciliation", race_result == []
      and json.loads(race_marker.read_text())["delivery_id"] == new_id,
      {"cleared": race_result, "delivery_id": json.loads(race_marker.read_text()).get("delivery_id")})

print(f"{PASS} passed, {FAIL} failed")
lib.thread_context_claims = _ORIGINAL_CLAIMS
sys.exit(1 if FAIL else 0)
