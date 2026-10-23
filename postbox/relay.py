#!/usr/bin/env python3
"""Post box relay: the always-on program that turns "mail arrived" into "the right session wakes".

Waiting is done here, by ordinary software, never by an AI. Mail is taken from the NATS stream,
delivered through the receiving tool's own door, and only then confirmed (so a crash means redelivery).

Mail from another Mac is not trusted beyond its text: the sender's address is cleaned, its process number must be digits, and the reply
inbox it names must be a real _INBOX name (this relay may publish anywhere, so a hostile name must never become a subject).

Telemetry (metadata only, never a message body; see telemetry.py; off unless POSTBOX_TELEMETRY=1): every decision on
mail is written as a post_deliver event (retries of mail that keeps waiting are only counted), expired mail as post_expire, and ONE
post_relay_summary per hour says what happened, so silence can be told apart from broken telemetry.

Mail for a Mac that was asleep when it was sent is kept here as forward.<machine>.<project>.<agent> (see post.py). The forwarder below hands it
to that Mac's own relay as soon as the Mac answers, and tells nobody: the sender's command ended long ago. Mail that waited longer than
lib.STALE_AFTER_S (by its Sent-At header, else by how long the stream held it) reaches the receiver with a "ask the sender first" banner.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nats  # noqa: E402
from nats.errors import TimeoutError as NatsTimeout  # noqa: E402
from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig  # noqa: E402
from nats.js.errors import NotFoundError  # noqa: E402

import lib  # noqa: E402
import wake  # noqa: E402

LIMITER = lib.PairLimiter()
RECIPIENT_LIMITER = lib.PairLimiter(limit=lib.RECIPIENT_LIMIT)
MAX_MSG_BYTES = lib.MAX_TEXT_BYTES * 2      # the stream itself refuses anything bigger (text plus headers)

WAKER = wake.Waker(log=lambda event, **kv: log(event, **kv))   # the doorbell for inbox-only agents that have an entry in wake.json
tel = lib.telemetry
SUMMARY_EVERY_S = int(os.environ.get("POSTBOX_SUMMARY_S", "3600"))   # one post_relay_summary per this many seconds
# Quiet work (heartbeat, Codex checks, expiry sweeps) runs at least this often, even while mail keeps arriving.
QUIET_EVERY_S = float(os.environ.get("POSTBOX_QUIET_S", "25"))
EXPIRY_GRACE_S = 60                                                   # mail still unclaimed this long after the stream's own age limit is counted as expired
ERROR_EVENTS = ("relay_error", "handle_error", "who_error", "reply_failed")
ERROR_REPEAT_S = 60                                                   # the same error class is written to telemetry at most once per this many seconds
QUEUED_MAX = 5000
FORWARD_RETRY_S = int(os.environ.get("POSTBOX_FORWARD_RETRY_S", "60"))   # a forward that could not be handed over is tried again after this many seconds
FORWARD_CONNECT_S = 3           # how long one try to open the other Mac's mailbox may take
FORWARD_REPLY_S = 8             # how long to wait for the other Mac's relay to answer a forwarded message
FORWARD_LIMIT_REASONS = ("pair_limit", "recipient_limit")   # the other Mac refused for a stop rule: that clears with time, so the mail is kept and tried again


class RelayStats:
    """What the relay did since its last hourly summary. Kept in memory; the post_relay_summary event is the durable record."""

    def __init__(self):
        self.started = time.time()      # this process's start, for uptime
        self.reconnects = 0
        self.reset()

    def reset(self):
        self.since = time.time()
        self.results = collections.Counter()     # (result, door) -> decisions written (probes excluded)
        self.reasons = collections.Counter()     # refusal / failure / queue reasons
        self.logs = collections.Counter()        # relay log event -> count (all of them, heartbeats included)
        self.errors = collections.Counter()      # "event:class" -> count (before throttling)
        self.retry_naks = 0                      # redeliveries of waiting mail that were NOT written as events
        self.probes = 0                          # selfcheck messages (the health check's test mail)
        self.cross_in = 0                        # decisions on mail that came from another Mac
        self.expired_queue = 0
        self.delivered_after_queue = 0
        self.forwards = collections.Counter()    # post_forward status -> count (stored, forwarded, refused, expired); retries are not counted
        self.queue_ages = []                     # seconds waited, for mail delivered after it had to wait
        self.handle_ms = []                      # time inside handle() for delivered mail

    def summary(self):
        by_door, totals = {}, collections.Counter()
        for (result, door), n in self.results.items():
            by_door.setdefault(door, {})[result] = n
            totals[result] += n
        ages = sorted(self.queue_ages)
        times = sorted(self.handle_ms)
        return {
            "window_s": int(time.time() - self.since), "uptime_s": int(time.time() - self.started), "reconnects": self.reconnects,
            "delivered": totals["delivered"], "queued": totals["queued"], "refused": totals["refused"], "failed": totals["failed"], "held": totals["held"],
            "by_door": by_door, "reasons": dict(self.reasons), "retry_naks": self.retry_naks, "probes": self.probes,
            "cross_machine_in": self.cross_in, "expired_queue": self.expired_queue, "delivered_after_queue": self.delivered_after_queue,
            "forwards": dict(self.forwards),
            "queue_wait_max_s": int(ages[-1]) if ages else None, "handle_ms_p50": times[len(times) // 2] if times else None,
            "handle_ms_max": times[-1] if times else None, "heartbeats": self.logs.get("heartbeat", 0),
            "log_counts": {k: v for k, v in self.logs.items() if k != "heartbeat"}, "errors": dict(self.errors),
        }


STATS = RelayStats()
QUEUED: dict = {}      # stream sequence -> what is known about mail that is waiting (rebuilt by the next redelivery after a restart)
FWD_WAITING: dict = {}  # stream sequence -> what is known about forward mail that is waiting for its Mac (rebuilt the same way)
PEER_DOWN: dict = {}   # machine -> when opening its mailbox last failed: its other waiting mail is not tried again at once (one try per Mac per round)
ERR_LAST: dict = {}    # (event, class) -> [last time written, how many were held back since]


def log(event, **kv):
    print(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **kv}), flush=True)
    try:   # telemetry must never be able to break logging
        STATS.logs[event] += 1
        if event in ERROR_EVENTS:
            relay_error_event(event, kv.get("error"))
    except Exception:  # noqa: BLE001
        pass


def relay_error_event(event, error):
    """post_relay_error: an error CLASS (never the error text), at most once per minute per class; the rest is counted."""
    text = str(error)
    cls = "auth_violation" if ("uthorization" in text or "uthentication" in text) else tel.error_class_of_text(text)
    STATS.errors[f"{event}:{cls}"] += 1
    now, key = time.time(), (event, cls)
    last = ERR_LAST.get(key)
    if last and now - last[0] < ERROR_REPEAT_S:
        last[1] += 1
        return
    ERR_LAST[key] = [now, 0]
    tel.emit("post_relay_error", "relay", status="error", kind=event, error_class=cls, held_back=last[1] if last else 0)


def msg_facts(m):
    """(times delivered, seconds since the stream stored it, stream sequence) from NATS's own record; None for what a message does not say."""
    md = getattr(m, "metadata", None)
    stored = getattr(md, "timestamp", None)
    try:
        age = round(max(0.0, time.time() - stored.timestamp()), 1) if stored is not None else None
    except Exception:  # noqa: BLE001
        age = None
    return getattr(md, "num_delivered", None), age, getattr(getattr(md, "sequence", None), "stream", None)


def message_age_s(hdr, queue_age, now=None):
    """How long a message has been on its way, in seconds: from its Sent-At header (the ORIGINAL send time, which survives forwarding)
    when that is a plausible time, else from how long the stream has held it (None when that is unknown too). A garbage Sent-At is ignored."""
    now = time.time() if now is None else now
    text = str((hdr or {}).get("Sent-At", ""))
    if re.fullmatch(r"[0-9]{9,11}", text):
        sent = int(text)
        if 1_500_000_000 <= sent <= now + 300:   # not from before 2017 and not from the future (a few minutes of clock difference is allowed)
            return max(0, int(now - sent))
    return queue_age


def busy_kinds(notes):
    return [kind for needle, kind in (("is mid-task", "mid_task"), ("cannot be woken", "cannot_wake")) if any(needle in str(n) for n in notes)]


def decide(m, result, reason, started, facts, first_try, **extra):
    """Count one relay decision and write its post_deliver event. Mail that keeps waiting is retried every 10 seconds; those
    retries are only counted (retry_naks). Written: the first decision on a message, every refusal, and the final delivery."""
    try:
        probe = bool(facts.get("probe"))
        _, age, seq = msg_facts(m)
        waiting = result in ("queued", "failed")
        if waiting and seq is not None and len(QUEUED) < QUEUED_MAX:   # also re-learns mail that was waiting before a relay restart
            QUEUED.setdefault(seq, {"stored": time.time() - (age or 0), "frm": facts.get("frm"), "to": facts.get("to"), "door": facts.get("door")})
        if waiting and not first_try:
            STATS.retry_naks += 1
            return
        if probe:
            STATS.probes += 1
        else:
            STATS.results[(result, facts.get("door", "unknown"))] += 1
            if reason:
                STATS.reasons[reason] += 1
            if facts.get("cross_machine"):
                STATS.cross_in += 1
        if not waiting:
            QUEUED.pop(seq, None)
            if result == "delivered" and (facts.get("attempts") or 1) > 1:
                STATS.delivered_after_queue += 1
                if age is not None and len(STATS.queue_ages) < 500:
                    STATS.queue_ages.append(age)
        if result == "delivered" and len(STATS.handle_ms) < 500:
            STATS.handle_ms.append(tel.elapsed_ms(started))
        status = "success" if probe else tel.status_for(result)   # a refused selfcheck is the answer the check expects
        tel.emit("post_deliver", "relay", status=status, duration_ms=tel.elapsed_ms(started), result=result, reason=reason,
                 limit=tel.limit_of(reason), **facts, **extra)
    except Exception:  # noqa: BLE001 - telemetry never changes what the relay does with mail
        pass


def sweep_expired_queue(now=None):
    """Mail that was waiting and is still unclaimed after the stream's age limit has expired inside NATS, which says nothing when it
    does. Report it once, as post_expire kind=queue. A relay restart forgets what was waiting, and the next redelivery re-learns it."""
    now = time.time() if now is None else now
    gone = {seq: v for seq, v in QUEUED.items() if now - v["stored"] > lib.MAX_AGE_S + EXPIRY_GRACE_S}
    if not gone:
        return 0
    for seq in gone:
        QUEUED.pop(seq, None)
    STATS.expired_queue += len(gone)
    tel.emit("post_expire", "relay", status="warning", kind="queue", count=len(gone), by_door=dict(collections.Counter(v["door"] for v in gone.values())),
             oldest_age_s=int(max(now - v["stored"] for v in gone.values())),
             items=[{"frm": v["frm"], "to": v["to"], "age_s": int(now - v["stored"])} for v in list(gone.values())[:10]])
    return len(gone)


def sweep_expired_forwards(now=None):
    """Forward mail still unclaimed after the stream's age limit has expired inside NATS, which says nothing when it does. Report it once
    as post_forward status=expired. A relay restart forgets what was waiting, and the next redelivery re-learns it."""
    now = time.time() if now is None else now
    gone = {seq: v for seq, v in FWD_WAITING.items() if now - v["stored"] > lib.MAX_AGE_S + EXPIRY_GRACE_S}
    if not gone:
        return 0
    for seq in gone:
        FWD_WAITING.pop(seq, None)
    STATS.forwards["expired"] += len(gone)
    tel.emit("post_forward", "relay", status="warning", forward_status="expired", count=len(gone), by_machine=dict(collections.Counter(v["machine"] for v in gone.values())),
             oldest_age_s=int(max(now - v["stored"] for v in gone.values())),
             items=[{"frm": v["frm"], "to": v["to"], "machine": v["machine"], "age_s": int(now - v["stored"])} for v in list(gone.values())[:10]])
    return len(gone)


async def queue_gauge(js):
    """How many messages the stream is holding right now (mail nobody has taken yet) and how old the oldest is. {} when it cannot be read."""
    try:
        state = (await asyncio.wait_for(js.stream_info(lib.STREAM), 3)).state
        out = {"queue_depth": state.messages}
        if state.messages and getattr(state, "first_seq", None):
            # nats-py's stream state has no timestamp, so the oldest waiting message is looked up by number; only its time is kept
            stored = getattr(await asyncio.wait_for(js.get_msg(lib.STREAM, seq=state.first_seq), 3), "time", None)
            if stored is not None:
                out["oldest_queued_age_s"] = int(max(0, time.time() - stored.timestamp()))
        return out
    except Exception:  # noqa: BLE001
        return {}


async def maybe_summary(js, force=False):
    """The hourly post_relay_summary: counts by result and door since the last one, queue and Grok-inbox depth, telemetry's own counters."""
    if not force and time.time() - STATS.since < SUMMARY_EVERY_S:
        return
    try:
        sweep_expired_queue()
        sweep_expired_forwards()
        depths = await asyncio.to_thread(lib.inbox_depths)
        gauge = await queue_gauge(js)
        tel.emit("post_relay_summary", "relay", **STATS.summary(), inbox_depth=sum(depths.values()),
                 inbox_depths=dict(sorted(depths.items(), key=lambda kv: -kv[1])[:10]), **gauge, telemetry=tel.stats())
    except Exception:  # noqa: BLE001
        pass
    STATS.reset()


def _seconds(value):
    """A stream age limit in seconds, whichever unit the library reports it in (nats-py 2.x: seconds; a library that hands over the server's
    nanoseconds gives a number no age limit in seconds could be)."""
    return float(value) / 1e9 if value and value > 10 ** 9 * 100 else float(value or 0)


async def ensure_stream(js):
    try:
        info = await js.stream_info(lib.STREAM)
    except NotFoundError:
        await js.add_stream(StreamConfig(
            name=lib.STREAM,
            subjects=["post.>", "forward.>"],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.FILE,
            max_age=lib.MAX_AGE_S,
            duplicate_window=lib.DUPE_WINDOW_S,
            max_msg_size=MAX_MSG_BYTES,
            description="Agent post box: post.<project>.<agent> (mail for this Mac) and forward.<machine>.<project>.<agent> (mail for a Mac that was asleep). Each message is consumed once. Rules: README.md in the post box repo",
        ))
        log("stream_created")
        return
    # a stream made by an older version: bring it up to date, one fix at a time, never failing the relay over any of them
    if getattr(info.config, "max_msg_size", None) in (None, -1, 0):
        try:
            cfg = info.config
            cfg.max_msg_size = MAX_MSG_BYTES
            await js.update_stream(cfg)
            log("stream_size_cap_added", bytes=MAX_MSG_BYTES)
        except Exception as e:  # noqa: BLE001
            log("relay_error", error="could not add the message size cap to the stream: " + repr(e)[:120])
    if _seconds(getattr(info.config, "max_age", None)) != lib.MAX_AGE_S:
        try:
            cfg = info.config
            cfg.max_age = lib.MAX_AGE_S
            await js.update_stream(cfg)
            log("stream_max_age_updated", seconds=lib.MAX_AGE_S)
        except Exception as e:  # noqa: BLE001
            log("relay_error", error="could not update the stream's age limit: " + repr(e)[:120])
    if "forward.>" not in (getattr(info.config, "subjects", None) or []):
        try:
            cfg = info.config
            cfg.subjects = list(cfg.subjects or []) + ["forward.>"]
            await js.update_stream(cfg)
            log("stream_subjects_updated", subjects=cfg.subjects)
        except Exception as e:  # noqa: BLE001
            log("relay_error", error="could not add forward.> to the stream: " + repr(e)[:120])


async def reply(nc, inbox, status, detail, targets=(), reason=None):
    """Answer the sender, only ever on a real reply inbox, and never let a failed answer stop the mail from being acknowledged.
    Returns what happened (sent, no_inbox, bad_inbox, failed) for the telemetry record. `reason` is the relay's own class for the answer
    (pair_limit, too_long, nobody_there, thread_not_ready ...); an older sender ignores it."""
    if not inbox:
        return "no_inbox"
    if not lib.INBOX_RE.match(str(inbox)):
        log("bad_reply_inbox", inbox=str(inbox)[:60])
        return "bad_inbox"
    try:
        body = {"status": status, "detail": detail, "targets": list(targets)}
        if reason:
            body["reason"] = reason
        await nc.publish(inbox, json.dumps(body).encode())
        await nc.flush(timeout=2)
    except Exception as e:  # noqa: BLE001
        log("reply_failed", error=repr(e)[:120])
        return "failed"
    return "sent"


def redirect_desk_mail(agent, project, from_addr):
    """The project whose Desk should get this mail instead of the address it was sent to, or None. Only for mail to grokbot-desktop from a session ON
    THIS MAC (agent@project, no machine part) whose project has its own wake entry (a Desk with its own webhook), and only when the address it was
    sent to is NOT itself a Desk: a general-purpose address, or a name nobody owns such as a typo (grokbot-desktop@grok-desktop). Mail to another real
    Desk is left alone. Projects whose own mail must never be redirected are listed in "desk_sender_projects" in the grokbot-desktop entry of wake.json
    (default: none). Switch the redirect off with "desk_redirect": false in the same entry. Only active when the grokbot-desktop door is on."""
    if lib.norm(agent) != "grokbot-desktop" or not lib.doors.enabled("grokbot-desktop"):
        return None
    parts = str(from_addr).split("@")
    if len(parts) != 2 or not parts[1]:
        return None
    sender_project, target = lib.canon_project(parts[1]), lib.canon_project(project)
    entry = WAKER.entry(agent) or {}
    listed = entry.get("desk_sender_projects")
    skipped = {lib.canon_project(p) for p in (listed if isinstance(listed, list) else []) if isinstance(p, str)}
    if sender_project == "unknown" or sender_project in skipped or entry.get("desk_redirect") is False or target == sender_project:
        return None
    if not WAKER.has_own_entry(agent, sender_project) or WAKER.has_own_entry(agent, target):
        return None
    return sender_project


async def ring_wake(door, agent, project, from_addr):
    """After an inbox write for an agent that has a wake entry: call its webhook ONCE and return the sentence for the sender's answer
    ("" when the agent has no wake entry, so its answer is exactly as before). The mail is already stored and acknowledged; whatever happens
    here only changes the sentence, never the delivery."""
    try:
        if not getattr(door, "inbox_only", False) or not WAKER.configured(agent, project):
            return ""
        waiting = len(await asyncio.to_thread(lib.inbox_list, agent, project))
        result = await asyncio.to_thread(WAKER.wake, agent, project, from_addr, waiting)
    except Exception as e:  # noqa: BLE001
        log("wake_failed", agent=agent, project=project, reason="error_" + type(e).__name__)
        return f" (wake webhook failed: error_{type(e).__name__}; it will read the inbox on its next turn)"
    if result is None:
        return ""
    if result.ok:
        return " and its wake webhook was called"
    return f" (wake webhook failed: {result.reason}; it will read the inbox on its next turn)"


async def handle(nc, m, doors=None, limiter=None, recipient_limiter=None):
    t_start = time.monotonic()
    doors = lib.DOORS if doors is None else doors
    limiter = LIMITER if limiter is None else limiter
    recipient_limiter = RECIPIENT_LIMITER if recipient_limiter is None else recipient_limiter
    parts = m.subject.split(".", 2)
    attempts, queue_age, _seq = msg_facts(m)
    if len(parts) != 3 or parts[0] != "post":
        log("bad_subject", subject=m.subject)
        await m.term()
        decide(m, "refused", "bad_subject", t_start, {"attempts": attempts, "queue_age_s": queue_age}, True)
        return
    _, project, agent = parts
    project = lib.canon_project(project)   # aliases.json: project-g and project-g-full are one project, whichever name the sender typed
    hdr = m.headers or {}
    from_addr = lib.clean_address(hdr.get("From", "unknown"))
    remote_sender = len(from_addr.split("@")) == 3
    pid_text = str(hdr.get("From-Pid", ""))
    from_pid = int(pid_text) if re.fullmatch(r"[0-9]{1,9}", pid_text) else None   # isdigit() also accepts characters int() rejects
    thread_text = str(hdr.get("From-Thread", ""))
    from_thread = thread_text if not remote_sender and lib.UUID_RE.fullmatch(thread_text) else None
    inbox = hdr.get("Reply-Inbox")
    bad_inbox = bool(inbox) and not lib.INBOX_RE.match(str(inbox))
    if bad_inbox:
        log("bad_reply_inbox", inbox=str(inbox)[:60])
        inbox = None
    first_try = m.metadata.num_delivered == 1
    text = m.data.decode("utf-8", "replace")
    asked_project = project
    redirected = redirect_desk_mail(agent, project, from_addr)
    if redirected:   # mail from a project that has its own Desk, addressed to the general Desk: the project's own Desk is the right inbox
        log("desk_redirect", frm=from_addr, to=f"{agent}@{project}", new_to=f"{agent}@{redirected}")
        project = redirected
    to = f"{agent}@{project}"
    sender = from_addr.split("@")
    facts = {"frm": from_addr, "to": to, "to_agent": agent, "to_project": project, "door": agent,
             "cross_machine": len(sender) == 3, "peer": sender[2] if len(sender) == 3 else None,
             "probe": (project, agent) == ("selfcheck", "selfcheck"), "attempts": attempts, "queue_age_s": queue_age,
             "bad_reply_inbox": bad_inbox, **tel.text_facts(text), "bytes": len(m.data)}
    waited = message_age_s(hdr, queue_age)
    stale = waited is not None and waited > lib.STALE_AFTER_S
    if stale:
        facts.update(stale=True, waited_s=int(waited))
    # per-session addressing (README: "Which session"): the sender's session name shows in the reply command, and To-Session names the one session
    # the message is for. Both are cleaned like every other name; Pick is the id of a held message to send on.
    from_session = lib.valid_session(hdr.get("From-Session")) if hdr.get("From-Session") else None
    from_label = from_addr + (f"#{from_session}" if from_session and from_addr != "unknown" else "")
    late = not first_try or hdr.get("Forwarded") == "1"   # the sender's command is over: an answer it cannot hear needs a notice
    want_raw, pick_id = hdr.get("To-Session"), str(hdr.get("Pick") or "")
    want = lib.valid_session(want_raw) if want_raw else None
    facts.update(session_named=bool(want_raw), picked=bool(pick_id))

    if len(m.data) > lib.MAX_TEXT_BYTES:
        log("refused_too_long", frm=from_addr, to=to, bytes=len(m.data))
        await m.term()
        facts["reply"] = await reply(nc, inbox, "refused", f"message too long ({len(m.data)} bytes, the limit is {lib.MAX_TEXT_BYTES}); put the detail in a file and send its path",
                                     reason="too_long")
        decide(m, "refused", "too_long", t_start, facts, first_try)
        return
    # the stop rules count NEW mail only: a queued message is redelivered every 10 s, and counting those would drop it after about 5 minutes
    if first_try and not limiter.allow((from_addr, to)):
        log("refused_rate_limit", frm=from_addr, to=to)
        await m.term()
        facts["reply"] = await reply(nc, inbox, "refused", f"stop rule: more than {limiter.limit} messages from {from_addr} to {to} in {limiter.window // 60} minutes",
                                     reason="pair_limit")
        decide(m, "refused", "pair_limit", t_start, facts, first_try, limit_value=limiter.limit)
        return
    if first_try and not recipient_limiter.allow(("all senders", to)):
        log("refused_recipient_limit", to=to)
        await m.term()
        facts["reply"] = await reply(nc, inbox, "refused", f"stop rule: more than {recipient_limiter.limit} messages to {to} in {recipient_limiter.window // 60} minutes from all senders together",
                                     reason="recipient_limit")
        decide(m, "refused", "recipient_limit", t_start, facts, first_try, limit_value=recipient_limiter.limit)
        return
    door = doors.get(agent)
    if door is None:
        log("refused_no_door", to=to)
        await m.term()
        facts["reply"] = await reply(nc, inbox, "refused", f"no delivery door for agent '{agent}' is switched on (doors on: {', '.join(sorted(doors))}; see postbox/doors.py)", reason="no_door")
        decide(m, "refused", "no_door", t_start, facts, first_try)
        return

    my_machine = lib.my_machine()
    machine_part = my_machine if len(sender) == 3 else None   # a sender on another Mac must be told commands that reach THIS Mac

    async def refuse(reason, detail, event, notice=False, **extra):
        """Refuse this mail for good (it is dropped from the queue) and say why; `notice`: also tell a sender whose command is already over."""
        log(event, frm=from_addr, to=to, reason=reason)
        await m.term()
        facts["reply"] = await reply(nc, inbox, "refused", detail, reason=reason)
        decide(m, "refused", reason, t_start, facts, first_try, **extra)
        if notice and late:
            await notify_sender(nc, doors, from_addr, from_session, f"Post box notice (no reply needed): your message to {to} was NOT delivered. {detail}")

    if want_raw and want is None:
        await refuse("bad_session", f"'{str(want_raw)[:40]}' is not a usable session name: use the name `post who` shows after the #, letters, digits and hyphens", "refused_bad_session")
        return
    held = None
    if pick_id:   # post pick <id> <to>: send a held message on to ONE session. The pick message itself carries no text of its own.
        held = await asyncio.to_thread(lib.hold_get, pick_id)
        if held is None:
            await refuse("pick_unknown", f"no held message '{pick_id[:16]}' on {my_machine} (it was already sent on, it expired after {lib.ago(lib.MAX_AGE_S)}, or it is held on another Mac): `post held` lists what is held here", "refused_pick")
            return
        if held.get("frm") != from_addr:
            await refuse("pick_not_yours", f"held message {held['id']} was sent by {held.get('frm')}, not by {from_addr}: only its sender can send it on", "refused_pick")
            return
        if held.get("agent") != agent or lib.canon_project(held.get("project")) != project:
            await refuse("pick_mismatch", f"held message {held['id']} is for {held.get('agent')}@{lib.canon_project(held.get('project'))}, not {to}: name that session instead", "refused_pick")
            return
        if want is None:
            await refuse("pick_no_session", f"post pick needs the session to send it to, after a #: post pick {held['id']} {agent}@{project}#<name>", "refused_pick")
            return
        text = held["text"]   # the HELD message is what gets delivered, as its original sender wrote it
        from_label = held["frm"] + (f"#{held['session']}" if held.get("session") else "")
        stale = False         # a person or agent just chose the session: the message is current, a "still relevant?" banner would only get in the way
        facts.pop("stale", None)
        facts.pop("waited_s", None)
        facts.update(tel.text_facts(text), bytes=len(text.encode("utf-8", "replace")))

    t_resolve = time.monotonic()
    if agent == "codex" and from_thread:
        targets = await asyncio.to_thread(door.resolve, project, from_pid, None, from_thread)
    else:
        targets = await asyncio.to_thread(door.resolve, project, from_pid)
    facts["resolve_ms"] = tel.elapsed_ms(t_resolve)
    per_session = getattr(door, "per_session", False)
    route = None
    unresolved = await asyncio.to_thread(door.unresolved, project, from_pid) if agent == "codex" and hasattr(door, "unresolved") and not want else []
    if unresolved and targets:
        if first_try:
            log("claim_not_ready", frm=from_addr, to=to, pids=[w.pid for w in unresolved])
            facts["reply"] = await reply(nc, inbox, "queued", f"an unresolved Codex sibling screen is open in {project}; kept for {lib.ago(lib.MAX_AGE_S)} until its exact conversation is known",
                                         reason="claim_not_ready")
        await m.nak(delay=10)
        decide(m, "queued", "claim_not_ready", t_start, facts, first_try, targets_n=0, waiting_pids=[w.pid for w in unresolved])
        return
    if not targets and pick_id:
        await refuse("pick_nobody", f"no {agent} session is open in {project} right now, so held message {held['id']} stays held: try `post pick` again when one is", "refused_pick")
        return
    if not targets:
        waiting = await asyncio.to_thread(door.waiting, project, from_pid) if hasattr(door, "waiting") else []
        if waiting:   # the screen is open but has not started its own conversation: mail would go into an older, dead thread
            if first_try:   # a waiting message is redelivered every 10 s on purpose; only the first decision is logged (retries are counted in decide)
                log("thread_not_ready", frm=from_addr, to=to, first_try=first_try, pids=[w.pid for w in waiting])
                facts["reply"] = await reply(nc, inbox, "queued", f"the {agent.title()} screen in {project} has not started a conversation yet; kept for {lib.ago(lib.MAX_AGE_S)} and delivered when it does",
                                             reason="thread_not_ready")
            await m.nak(delay=10)
            decide(m, "queued", "thread_not_ready", t_start, facts, first_try, targets_n=0, waiting_pids=[w.pid for w in waiting])
            return
        if first_try:   # same rule: one log line per message, not one per 10-second redelivery (otherwise one queued message looks like a send loop)
            log("nobody_there", frm=from_addr, to=to, first_try=first_try)
            facts["reply"] = await reply(nc, inbox, "queued", f"nobody reachable for {to} right now; kept for {lib.ago(lib.MAX_AGE_S)} and delivered when a session appears",
                                         reason="nobody_there")
        await m.nak(delay=10)
        decide(m, "queued", "nobody_there", t_start, facts, first_try, targets_n=0)
        return

    if per_session:   # Claude and Codex: ONE session gets the message. Exactly one open, or the one named; anything else is held or refused, never "everyone".
        open_n = len(targets)
        outcome, chosen = lib.choose_sessions(targets, want)
        facts["sessions_n"] = open_n
        if outcome in ("one", "named"):
            targets = chosen
            route = "picked" if pick_id else "delivered_one" if outcome == "one" else "delivered_named"
        elif outcome == "unknown":
            await refuse("pick_unknown_session" if pick_id else "unknown_session", lib.unknown_session_text(agent, project, want, targets, machine_part), "refused_unknown_session",
                         notice=not pick_id, route="refused_unknown_session")
            return
        elif pick_id:   # two sessions answer to that name: the short id tells them apart
            await refuse("pick_ambiguous", f"{len(chosen)} open sessions answer to '{want}': pick by short id. " + " or ".join(lib.pick_commands(held["id"], agent, project, chosen, machine_part)),
                         "refused_pick", route="refused_ambiguous")
            return
        else:   # nothing in the message says which session: keep it, tell the sender, never deliver it to several
            hold_id = None
            try:
                hold_id = await asyncio.to_thread(lib.hold_put, {"msg_id": hdr.get("Nats-Msg-Id") or f"seq-{_seq}-{m.subject}", "agent": agent, "project": project, "frm": from_addr,
                                                                  "session": from_session or "", "pid": from_pid, "text": text,
                                                                  "sent_at": int(time.time() - waited) if waited is not None else int(time.time())})
            except lib.HoldFull as e:
                await refuse("hold_full", f"{e}; send it to one session directly: post {agent}@{project}#<name> \"...\" (open now: {lib.session_names(chosen)})", "refused_hold_full",
                             route="refused_hold_full")
                return
            await m.ack()
            detail = lib.bounce_text(hold_id, agent, project, chosen, machine_part, ambiguous_name=want if outcome == "ambiguous" else None)
            log("held", frm=from_addr, to=to, hold=hold_id, sessions=open_n, ambiguous=outcome == "ambiguous")
            facts["reply"] = await reply(nc, inbox, "held", detail, [t.label for t in chosen], reason="ambiguous_name" if outcome == "ambiguous" else "several_sessions")
            decide(m, "held", "ambiguous_name" if outcome == "ambiguous" else "several_sessions", t_start, facts, first_try, targets_n=open_n, route="bounced_several", held_id=hold_id)
            if late:   # the sender's command is over (the mail waited, or it came through the forwarder): the same bounce, as a message to its session
                facts["notice"] = await notify_sender(nc, doors, from_addr, from_session, "Post box notice (no reply needed): " + detail)
            return
    elif want:
        facts["session_ignored"] = True   # grok and grokbot-desktop have one inbox per project: a #name changes nothing for them

    render = getattr(door, "render", None) or lib.render   # a door may word the reading instructions for its own agent (Grok, grokbot-desktop: their inbox)
    kw = {"project": project} if getattr(door, "render_with_project", False) else {}   # grokbot-desktop must state who it is, so its commands name the project
    body = render(from_label, text, waited_s=int(waited), **kw) if stale else render(from_label, text, **kw)
    delivered, failed, notes, fail_classes = [], [], [], []
    t_deliver = time.monotonic()
    for t in targets:
        note = ""
        busy_note = getattr(door, "busy_note", None)
        if busy_note:  # asked BEFORE delivering; a failed look must never block the delivery itself
            try:
                note = await asyncio.to_thread(busy_note, t)
            except Exception:  # noqa: BLE001
                note = ""
        try:
            await asyncio.to_thread(door.deliver, t, body)
            delivered.append(t.label)
            watch = getattr(door, "watch", None)
            if watch:   # Codex: prove the message reached the conversation that screen has open (codex_watch_check); never blocks delivery
                try:
                    await asyncio.to_thread(watch, t, text, from_addr, from_session)
                except Exception as e:  # noqa: BLE001
                    log("relay_error", error="codex watch failed: " + repr(e)[:120])
            if note:
                notes.append(note)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{t.label}: {e!r}")
            fail_classes.append(type(e).__name__)
    facts["deliver_ms"] = tel.elapsed_ms(t_deliver)
    shown = dict(targets_n=len(targets), target_pids=[getattr(t, "pid", None) for t in targets], failed_n=len(failed), busy=busy_kinds(notes))
    if route:
        shown["route"] = route
    if delivered:
        await m.ack()
        log("delivered", frm=from_addr, to=to, targets=delivered, failed=failed, bytes=len(text), busy=notes, route=route)
        detail = "delivered to " + ", ".join(delivered)
        if pick_id:
            await asyncio.to_thread(lib.hold_delete, pick_id)
            detail += f" (held message {pick_id} sent on)"
        woke = await ring_wake(door, agent, project, from_addr)
        if redirected:
            detail += f" (sent to this project's own Desk, {agent}@{project}, instead of {agent}@{asked_project}; use that address next time)"
        if woke:
            detail += woke
        elif notes:
            detail += ". Heads up: " + "; ".join(notes)
        if facts.get("session_ignored"):
            detail += f" ({agent} has one inbox per project, so the #{want} part was not used)"
        facts["reply"] = await reply(nc, inbox, "delivered", detail, delivered)
        decide(m, "delivered", None, t_start, facts, first_try, **shown)
    elif pick_id:   # a pick is never retried in the background: the message stays held and the person or agent can run the pick again
        log("delivery_failed", frm=from_addr, to=to, failed=failed, pick=pick_id)
        await m.term()
        facts["reply"] = await reply(nc, inbox, "refused", f"found the session but could not hand it the message; held message {pick_id} is still held, run the pick again: " + "; ".join(failed), reason="door_error")
        decide(m, "refused", "door_error", t_start, facts, first_try, fail_classes=sorted(set(fail_classes)), **shown)
    else:
        log("delivery_failed", frm=from_addr, to=to, failed=failed)
        why = "inbox_full" if "InboxFull" in fail_classes else "door_error"
        if first_try:
            facts["reply"] = await reply(nc, inbox, "queued", "found the session but could not hand it the message; retrying: " + "; ".join(failed), reason=why)
        await m.nak(delay=10)
        decide(m, "failed", why, t_start, facts, first_try, fail_classes=sorted(set(fail_classes)), **shown)


async def notify_sender(nc, doors, from_addr, from_session, notice):
    """Tell a sender whose command is already over what happened to its message (held, or refused for an unknown session). A sender on THIS
    Mac gets it as a message in its own session (found by name; if that cannot be told exactly, nobody is guessed); a sender on another Mac
    gets it through the forwarder, which hands it to that Mac's relay for its session. Returns sent, no_door, gone, or failed; never raises."""
    try:
        parts = str(from_addr).split("@")
        if len(parts) == 2 and parts[0] != "unknown":
            door = doors.get(parts[0])
            if door is None or not getattr(door, "per_session", False):
                return "no_door"
            sessions = await asyncio.to_thread(door.resolve, parts[1])
            outcome, chosen = lib.choose_sessions(sessions, from_session)
            if outcome not in ("one", "named"):
                return "gone"
            await asyncio.to_thread(door.deliver, chosen[0], notice)
            return "sent"
        if len(parts) == 3 and "unknown" not in parts:
            headers = {"From": f"postbox@{lib.my_machine()}", "Nats-Msg-Id": uuid.uuid4().hex, "Sent-At": str(int(time.time()))}
            if from_session:
                headers["To-Session"] = from_session
            await nc.jetstream().publish(f"forward.{parts[2]}.{parts[1]}.{parts[0]}", notice.encode(), headers=headers, timeout=5)
            return "sent"
    except Exception as e:  # noqa: BLE001 - a notice is a courtesy: the bounce itself was already recorded and answered
        log("notice_failed", error=repr(e)[:120])
        return "failed"
    return "gone"


ALARM_CMD = os.environ.get("POSTBOX_ALARM_CMD", "")   # optional: a script run as `bash <script> postbox-codex-unseen <title> <message> Basso 600` when a Codex message is not seen


async def check_codex_seen(nc, doors=None):
    """Settle watched Codex deliveries: `seen` is logged; `unseen` (the text never reached that conversation) is an alarm: logged, recorded in
    telemetry as an error, the sender is told in its own session, and, when POSTBOX_ALARM_CMD names a script, that script is run. Never raises."""
    if not lib.doors.enabled("codex"):
        return []   # the Codex door is off: nothing was ever watched
    doors = lib.DOORS if doors is None else doors
    try:
        settled = await asyncio.to_thread(lib.codex_watch_check)
    except Exception as e:  # noqa: BLE001
        log("relay_error", error="codex seen check failed: " + repr(e)[:120])
        return []
    for e, result in settled:
        waited = int(time.time() - float(e.get("sent", time.time())))
        if result == "seen":
            log("codex_seen", to=e.get("to"), frm=e.get("frm"), after_s=waited)
            tel.emit("post_codex_seen", "relay", status="ok", after_s=waited)
            continue
        log("codex_unseen", to=e.get("to"), frm=e.get("frm"), after_s=waited, thread=e.get("thread"))
        tel.emit("post_codex_unseen", "relay", status="error", after_s=waited)
        when = time.strftime("%H:%M", time.localtime(float(e.get("sent", time.time()))))
        notice = (f"Post box notice (no reply needed): your message to {e.get('to')} (sent {when}) was handed over, but after {waited // 60} min it "
                  f"has NOT appeared in the conversation that Codex screen has open, so Codex has most likely not seen it. Check `post who`, resend, "
                  f"and tell the person you work for if it happens again.")
        sent = await notify_sender(nc, doors, e.get("frm"), e.get("from_session") or None, notice)
        log("codex_unseen_notice", to=e.get("frm"), result=sent)
        if ALARM_CMD and os.path.exists(ALARM_CMD):
            title = f"{lib.my_machine().capitalize()} post box: Codex missed a message"
            try:
                await asyncio.to_thread(subprocess.run, ["bash", ALARM_CMD, "postbox-codex-unseen", title,
                                                         f"to {e.get('to')} from {e.get('frm')} at {when}", "Basso", "600"],
                                        capture_output=True, timeout=30)
            except Exception as ex:  # noqa: BLE001
                log("relay_error", error="codex unseen alarm failed: " + repr(ex)[:120])
    return settled


async def handle_forward(m, connect=None, peer_target=None):
    """One message on forward.<machine>.<project>.<agent>: hand it to that Mac's relay as post.<project>.<agent>, with its original From and
    Sent-At, wait for the answer, and acknowledge it WHATEVER the other Mac answered (it owns the mail now), except a stop-rule refusal, which
    clears with time. If the Mac cannot be reached the mail is kept and tried again in FORWARD_RETRY_S seconds, until the stream's age limit
    drops it. Nobody is told: the sender's command ended long ago. Telemetry: post_forward, metadata only."""
    t_start = time.monotonic()
    connect = connect or lib.connect_fast
    peer_target = peer_target or lib.peer_target
    parts = m.subject.split(".", 3)
    attempts, queue_age, seq = msg_facts(m)
    first_try = m.metadata.num_delivered == 1
    if len(parts) != 4 or parts[0] != "forward":
        log("bad_subject", subject=m.subject)
        await m.term()
        return
    _, machine, project, agent = parts
    hdr = m.headers or {}
    from_addr = lib.clean_address(hdr.get("From", "unknown"))
    to = f"{agent}@{project}"
    facts = {"frm": from_addr, "to": to, "to_machine": machine, **tel.text_facts(m.data.decode("utf-8", "replace")), "bytes": len(m.data),
             "attempts": attempts, "queue_age_s": queue_age}

    def emit(forward_status, status, **extra):
        tel.emit("post_forward", "relay", status=status, duration_ms=tel.elapsed_ms(t_start), forward_status=forward_status, **facts, **extra)

    async def keep(reason):
        """Keep the mail: it is tried again later. Written once (the first time); the retries are only counted, like queued mail."""
        await m.nak(delay=FORWARD_RETRY_S)
        if seq is not None and len(FWD_WAITING) < QUEUED_MAX:   # also re-learns mail that was waiting before a relay restart
            FWD_WAITING.setdefault(seq, {"stored": time.time() - (queue_age or 0), "frm": from_addr, "to": to, "machine": machine})
        if first_try:
            STATS.forwards["stored"] += 1
            log("forward_stored", frm=from_addr, to=to, machine=machine, reason=reason)
            emit("stored", "warning", reason=reason)
        else:
            STATS.retry_naks += 1

    if time.time() - PEER_DOWN.get(machine, 0) < FORWARD_RETRY_S / 2:   # that Mac just failed to answer another message: do not make this one wait through it too
        await keep("peer_unreachable")
        return
    try:
        target = await asyncio.to_thread(peer_target, machine)
    except lib.MailboxError as e:
        if tel.connect_error_class(e) == "peer_not_linked":   # no waiting will ever fix a Mac this one is not linked to
            log("refused_unknown_peer", machine=machine, frm=from_addr)
            await m.term()
            FWD_WAITING.pop(seq, None)
            STATS.forwards["refused"] += 1
            emit("refused", "blocked", reason="unknown_peer")
            return
        await keep("peer_setup")   # no login key yet, a name that does not resolve right now: fixable, so the mail waits
        return
    try:
        nc = await connect(target, timeout=FORWARD_CONNECT_S)
    except lib.MailboxError as e:
        PEER_DOWN[machine] = time.time()
        await keep("peer_refused_key" if e.refused else "peer_unreachable")
        return
    PEER_DOWN.pop(machine, None)
    answer = None
    try:
        inbox = nc.new_inbox()
        sub = await nc.subscribe(inbox)
        headers = {"From": from_addr, "Reply-Inbox": inbox, "Nats-Msg-Id": hdr.get("Nats-Msg-Id") or uuid.uuid4().hex}   # the original id: a republish after a crash is dropped as a duplicate
        if re.fullmatch(r"[0-9]{9,11}", str(hdr.get("Sent-At", ""))):
            headers["Sent-At"] = str(hdr["Sent-At"])
        for key in ("From-Session", "To-Session"):   # which session sent it and which one it is for survive forwarding; the cleaning is repeated by the receiving relay
            if lib.valid_session(hdr.get(key)):
                headers[key] = lib.valid_session(hdr.get(key))
        if lib.HOLD_ID_RE.match(str(hdr.get("Pick", ""))):
            headers["Pick"] = str(hdr["Pick"])
        headers["Forwarded"] = "1"   # the sender's command ended long ago: an answer it cannot hear needs a notice instead
        await nc.jetstream().publish(f"post.{project}.{agent}", m.data, headers=headers, timeout=5)
        try:
            answer = json.loads((await sub.next_msg(timeout=FORWARD_REPLY_S)).data)
        except Exception:  # noqa: BLE001 - stored on the other Mac, its relay just did not answer in time: the mail is theirs now
            answer = None
    except Exception:  # noqa: BLE001 - the connection broke before the other Mac stored it
        PEER_DOWN[machine] = time.time()
        await keep("publish_failed")
        return
    finally:
        try:
            await nc.close()
        except Exception:  # noqa: BLE001
            pass
    peer_status = str((answer or {}).get("status") or "no_answer")
    peer_reason = (answer or {}).get("reason")
    if peer_status == "refused" and peer_reason in FORWARD_LIMIT_REASONS:   # a stop rule on the other Mac clears with time: keep the mail instead of losing it
        await keep("peer_" + peer_reason)
        return
    await m.ack()
    FWD_WAITING.pop(seq, None)
    STATS.forwards["forwarded"] += 1
    log("forwarded", frm=from_addr, to=to, machine=machine, peer_status=peer_status, attempts=attempts)
    emit("forwarded", "success" if peer_status == "delivered" else "warning", peer_status=peer_status, peer_reason=peer_reason)


async def forward_loop(js):
    """The forwarder's own pull loop, next to the main one: it never blocks delivery of local mail, and a failure here is logged, not fatal."""
    try:
        fsub = await js.pull_subscribe("forward.>", durable="forwarder", stream=lib.STREAM, config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, ack_wait=30))
    except Exception as e:  # noqa: BLE001 - say so at once (the main loop also notices a stopped forwarder on its heartbeat)
        log("relay_error", error="forwarder could not start: " + repr(e)[:120])
        raise
    log("forwarder_ready")
    while True:
        try:
            msgs = await fsub.fetch(1, timeout=25)
        except NatsTimeout:
            continue
        for m in msgs:
            try:
                await handle_forward(m)
            except Exception as e:  # noqa: BLE001
                log("handle_error", error="forward: " + repr(e))
                await m.nak(delay=FORWARD_RETRY_S)


async def answer_who(nc, msg):
    """Another Mac asks `post who <this Mac>`: reply with the sessions that can be reached here (no delivery, nothing stored).
    Projects are sent in their address form, so what the other Mac prints is an address it can type."""
    if not msg.reply or not lib.INBOX_RE.match(msg.reply):
        return
    sessions = await asyncio.to_thread(lib.all_sessions)
    body = {"machine": lib.my_machine(), "sessions": [dict({"agent": s.agent, "project": lib.norm(s.project), "pid": s.pid},
                                                              **({"name": s.name} if s.name else {}), **({"sid": s.sid} if s.sid else {})) for s in sessions]}
    await nc.publish(msg.reply, json.dumps(body).encode())
    await nc.flush(timeout=2)
    tel.emit("post_who", "relay", side="answered", sessions_n=len(sessions), by_agent=dict(collections.Counter(s.agent for s in sessions)))


def throttled_error_logger():
    """A connection error callback that writes ONE relay_error line per distinct error per minute. Without it nats-py prints a traceback
    several times a second while a login is refused (about 20 MB a day), and the health check never sees an error line."""
    last = {"t": 0.0, "msg": ""}

    async def on_error(err=None):
        msg = repr(err)[:200]
        now = time.time()
        if msg != last["msg"] or now - last["t"] > 60:
            last.update(t=now, msg=msg)
            log("relay_error", error=msg)
    return on_error


async def main():
    quiet_rounds = 0
    ready_before = False
    last_quiet = 0.0
    while True:
        try:
            target = lib.local_target()
            break
        except lib.MailboxError as e:   # a damaged key file: say so in the log (the health check sees it) and look again later
            log("relay_error", error=str(e)[:200])
            await asyncio.sleep(30)
    on_error = throttled_error_logger()
    while True:
        nc = forwarder = None
        try:
            nc = await nats.connect(target.url, name="postbox-relay", max_reconnect_attempts=-1, error_cb=on_error, **target.auth())

            async def on_who(msg, nc=nc):
                try:
                    await answer_who(nc, msg)
                except Exception as e:  # noqa: BLE001 - a failed who must never take the relay down
                    log("who_error", error=repr(e))

            await nc.subscribe("who.sessions", cb=on_who)
            js = nc.jetstream()
            await ensure_stream(js)
            psub = await js.pull_subscribe(
                "post.>", durable="relay", stream=lib.STREAM,
                config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, ack_wait=30),
            )
            forwarder = asyncio.create_task(forward_loop(js))
            log("relay_ready", nats=target.url, machine=lib.my_machine(), login=bool(target.seed))
            if ready_before:
                STATS.reconnects += 1
            tel.emit("post_relay_start", "relay", reconnect=ready_before, reconnects=STATS.reconnects, login=bool(target.seed),
                     uptime_s=int(time.time() - STATS.started), summary_every_s=SUMMARY_EVERY_S)
            ready_before = True
            while True:
                log("idle_wait")   # the loop is about to wait on NATS; if the next line never comes, fetch is the stall
                msgs = []
                try:
                    msgs = await psub.fetch(1, timeout=25)
                    log("idle_wake", msgs=len(msgs))
                except NatsTimeout:
                    pass
                # A message that keeps waiting is redelivered every 10 s, so fetch never times out and the quiet work below
                # never ran (the heartbeat and Codex checks stopped for hours). Run it on a timer too.
                if not msgs or time.monotonic() - last_quiet >= QUIET_EVERY_S:
                    last_quiet = time.monotonic()
                    quiet_rounds += 1
                    log("idle_quiet_start", round=quiet_rounds)   # quiet branch: the codex check runs next
                    await check_codex_seen(nc)
                    log("idle_quiet_codex_done")
                    if quiet_rounds % 12 == 0:
                        log("heartbeat")
                        try:   # unread inbox mail expires like queued mail (lib.MAX_AGE_S); a failed sweep must never stop the relay
                            expired = await asyncio.to_thread(lib.inbox_sweep)
                            if expired:
                                log("inbox_expired", messages=expired)
                        except Exception as e:  # noqa: BLE001
                            log("relay_error", error="inbox sweep failed: " + repr(e)[:120])
                        try:   # a declaration (post join) whose session is gone or whose process number was reused; same never-fail pattern
                            swept = await asyncio.to_thread(lib.reg_sweep)
                            if swept:
                                log("registrations_swept", files=swept)
                        except Exception as e:  # noqa: BLE001
                            log("relay_error", error="registration sweep failed: " + repr(e)[:120])
                        try:   # a held message (it did not say which session) expires after the same 7 days as queued mail
                            dropped = await asyncio.to_thread(lib.hold_sweep)
                            if dropped:
                                log("holds_expired", messages=dropped)
                        except Exception as e:  # noqa: BLE001
                            log("relay_error", error="hold sweep failed: " + repr(e)[:120])
                        sweep_expired_queue()
                        sweep_expired_forwards()
                        if forwarder.done():   # the forwarder must not die quietly: say so, and let the reconnect below start a new one
                            raise RuntimeError("the forwarder stopped: " + repr(forwarder.exception() if not forwarder.cancelled() else "cancelled")[:120])
                    await maybe_summary(js)
                    log("idle_quiet_end", round=quiet_rounds)
                for m in msgs:
                    try:
                        await handle(nc, m)
                    except Exception as e:  # noqa: BLE001
                        log("handle_error", error=repr(e))
                        await m.nak(delay=10)
                await maybe_summary(js)
        except Exception as e:  # noqa: BLE001
            log("relay_error", error=repr(e))
            await asyncio.sleep(3)
        finally:
            if forwarder is not None:
                forwarder.cancel()
            if nc is not None:   # never leave the old connection behind when the loop reconnects (it kept answering who, twice)
                try:
                    await nc.close()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    asyncio.run(main())
