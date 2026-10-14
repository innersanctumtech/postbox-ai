#!/usr/bin/env python3
"""post: send a plain-text message to another agent session through the post box.

  post <to> <message...>     send. <to> is an agent (same project), agent@project, or agent@project@machine
  post <to> -                read the message from standard input
  post agent@project#name .. send to ONE session of that project, by the name `post who` shows; on another Mac: agent@project@machine#name
  post pick <id> <to>        send a message that was HELD (it did not say which session) to the session named in <to>, without retyping it
  post held                  list the messages held on this Mac, with the command that sends each one on
  post who [machine]         list the agent sessions that can be reached (on this Mac, or on another linked Mac)
  post inbox --history       show the messages this session already read in the last 7 days (nothing is cleared)
  post inbox                 print and clear the messages waiting for THIS session (an agent that cannot be woken reads mail this way)
  post inbox --watch         stay running and print one short line each time new mail arrives; it never clears anything
  post whoami                say who post thinks you are, and how it found out
  post wake-test <agent>@<project>   call that inbox-only agent's wake webhook once (event "test") and say sent or failed; needs an entry in wake.json
                             (only when the grokbot-desktop door is on, see postbox/doors.py)
  post join <project>        make THIS session a member of <project> (and of no other) for as long as it runs; `post join` alone says which project
                             it belongs to now and why; `post leave` undoes it. A `.postbox-project` file in a folder (first line = project name)
                             does the same for every session started below that folder
  post --as agent@project .. use that sender name for this one command, when post cannot tell which session it is running in
                             (env POSTBOX_AS=agent@project does the same, but only when no session is detected)

Examples:
  post claude "the migration finished, please re-run the tests"
  post claude@project-b "which file did you change for the login bug?"
  post claude@project-a@mac-b "did the deploy finish?"        the session on ANOTHER Mac; claude@@mac-b = my own project there

Only the agents whose door is switched on in postbox/doors.py can receive mail; `post --help` ends with the list.
Sending is instant and free of AI cost. The other session wakes on its own and answers with `post`.
Exit codes: 0 delivered, 2 could not send, 3 stored but nobody reachable yet (or, for another Mac, stored here to be forwarded), 4 refused,
5 held (several sessions are open in that project and the message did not say which: the answer lists them and the `post pick` command for each).
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib  # noqa: E402
import wake  # noqa: E402

tel = lib.telemetry   # metadata-only telemetry (postbox/telemetry.py); it never raises and never reads or stores a message body

# Facts about THIS run, filled in as the code learns them. run() turns them into exactly one telemetry event when the command ends,
# however it ends (success, die(), a refusal, a crash), so no exit path can forget to report.
CMD = {}
MAIL_EXTRA = {}   # the per-session headers of the message being sent (From-Session, To-Session, Pick); filled by main(), read by send()
T0 = time.monotonic()
EVENT_OF = {"send": "post_send", "who": "post_who", "whoami": "post_whoami", "inbox": "post_inbox", "join": "post_join", "leave": "post_leave", "held": "post_held"}
RESERVED = ("event_type", "component_name", "status", "duration_ms", "error_message", "session_id")   # emit()'s own parameter names


def note(**facts):
    CMD.update(facts)


def die(msg, code=2, reason="error", **facts):
    note(reason=reason, **facts)
    print(f"post: {msg}", file=sys.stderr)
    sys.exit(code)


def emit_command(code):
    """Write the one telemetry event for this command (post_send, post_who, post_whoami, post_inbox, post_join or post_leave) and mark it written."""
    command = CMD.get("command")
    if command not in EVENT_OF:
        return
    facts = {k: v for k, v in CMD.items() if k not in RESERVED and k != "command"}
    facts["exit_code"] = code
    total = tel.elapsed_ms(T0)
    facts["total_ms"] = total
    facts.setdefault("result", ("delivered" if command == "send" else "ok") if code == 0 else "error")
    CMD["command"] = None   # written: nothing may write it a second time
    error = facts.get("reason") if facts["result"] in ("error", "refused") else None
    tel.emit(EVENT_OF[command], "post-cli", status=tel.status_for(facts["result"]), duration_ms=total, error_message=error, **facts)


def usage_text():
    """The help text, followed by which doors are on. Lines about a switched-off agent are dropped from the examples."""
    text = __doc__
    if not lib.doors.enabled("grokbot-desktop"):
        text = "\n".join(line for line in text.splitlines() if "wake-test" not in line and "only when the grokbot-desktop door is on" not in line) + "\n"
    return text.rstrip("\n") + f"\n\nDoors on: {lib.doors.describe()}\n"


def run(argv):
    """main() plus exactly one telemetry event about it. Telemetry can never change what the command prints or its exit code."""
    CMD.clear()
    global T0
    T0 = time.monotonic()
    code = 0
    try:
        main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        raise
    except BaseException:
        code = 1
        raise
    finally:
        try:
            emit_command(code)
        except Exception:  # noqa: BLE001
            pass


def identity_problem(kind):
    """post_identity: a command could not find out who is sending (the case the statistics must explain)."""
    tel.emit("post_identity", "post-cli", status="error", reason=kind, command=CMD.get("command"),
             failed=list(lib.IDENTITY_TRACE.get("failed") or []), harness_hint=tel.harness_hint(), explicit_as=CMD.get("explicit_as"))


def link_event(peer, op, outcome, error_class, started):
    """post_link: one attempt to open another Mac's mailbox (outcome ok, unreachable, refused or setup), so unreachable-Mac episodes can be counted."""
    tel.emit("post_link", "post-cli", status="success" if outcome == "ok" else "error", duration_ms=tel.elapsed_ms(started),
             peer=peer, op=op, outcome=outcome, error_class=error_class, direction="out")


def link_outcome(exc):
    """refused (a key was rejected), setup (needs a person: unknown Mac, no key, damaged key) or unreachable (needs only time; includes a linked
    Mac's name that does not resolve right now, MailboxError.transient)."""
    if getattr(exc, "refused", False):
        return "refused"
    if getattr(exc, "setup", False) and not getattr(exc, "transient", False):
        return "setup"
    return "unreachable"


def remote_problem(machine, err):
    """Plain words for why another Mac's post box could not be used."""
    if err.setup:
        return str(err)
    if err.refused:
        return (f"{machine}'s post box refused this Mac's login key: {machine} does not know this Mac yet. "
                f"Run install.py --link on {machine} too (post box README, section Link two Macs).")
    return f"cannot reach {machine}'s post box ({err}). {machine} may be asleep, offline or not linked yet."


async def remote_who(machine):
    nc = await lib.connect_fast(lib.peer_target(machine), timeout=3)
    try:
        reply = await nc.request("who.sessions", b"", timeout=6)
    finally:
        await nc.close()
    return json.loads(reply.data)


def cmd_who(rest):
    machine = lib.norm(rest[0]) if rest else None
    if machine and machine != lib.my_machine():
        note(remote=True, peer=machine)
        started = time.monotonic()
        try:
            info = asyncio.run(asyncio.wait_for(remote_who(machine), 20))
        except lib.MailboxError as e:
            cls = tel.connect_error_class(e)
            link_event(machine, "who", link_outcome(e), cls, started)
            die(remote_problem(machine, e), reason="peer_" + link_outcome(e), error_class=cls)
        except asyncio.TimeoutError:
            link_event(machine, "who", "unreachable", "timeout", started)
            die(f"timed out asking {machine} who is there (20 s); it may be asleep", reason="peer_unreachable", error_class="timeout")
        except Exception as e:  # noqa: BLE001 - no responder, bad answer: say so in words
            link_event(machine, "who", "unreachable", "no_answer", started)
            die(f"{machine} did not answer the who question ({e!r}); its post box may be an older version: run install.py there",
                reason="peer_no_answer", error_class=tel.error_class(e))
        link_event(machine, "who", "ok", None, started)
        listed = info.get("sessions", [])
        note(sessions_n=len(listed), by_agent=dict(collections.Counter(str(s.get("agent")) for s in listed)))
        print(f"Reachable now on {machine} (agent@project@{machine}):")
        for s in info.get("sessions", []):
            who_named = lib.valid_session(s.get("name") or s.get("sid"))
            print(f"  {s['agent']}@{s['project']}@{machine}" + (f"#{who_named}" if who_named else ""))
        if not info.get("sessions"):
            print("  (none)")
        return
    me = lib.my_identity()
    note(identity_method=lib.IDENTITY_TRACE.get("method"), remote=False)
    print("Reachable now (agent@project):")
    found_at = time.monotonic()
    sessions = lib.all_sessions()
    note(sessions_n=len(sessions), by_agent=dict(collections.Counter(s.agent for s in sessions)), discover_ms=tel.elapsed_ms(found_at))
    for s in sessions:
        mark = "   <- this session" if me and s.pid == me[2] else ""
        shown = lib.norm(s.project)   # the address you can type; the folder name follows when it differs
        folder_name = os.path.basename(s.cwd.rstrip("/")) or s.cwd
        folder = f"  [folder {folder_name}]" if shown != folder_name else ""
        idle = "" if s.ready else "  (no conversation yet)"
        tag = {"declaration": "  (declared)", "marker": "  (marker)"}.get(s.source, "")
        named = f"  #{s.name or s.sid}" if (s.name or s.sid) else ""
        print(f"  {s.agent}@{shown:<24} pid {s.pid}{named}{folder}{idle}{tag}{mark}")
        if s.note:
            print(f"      note: {s.note}")
    inbox_agents = lib.inbox_only_agents()   # agents the box cannot discover (grokbot-desktop): listed only once their inbox exists
    wake_entries = wake.load_wake()
    for agent, project, waiting in inbox_agents:
        print(f"  {agent}@{project:<24} (inbox, {waiting} waiting; cannot be woken)  wake: {'configured (own webhook)' if wake.wake_key(agent, project) in wake_entries else 'configured' if wake.wake_key(agent) in wake_entries else 'not configured'}")
    if not sessions and not inbox_agents:
        print("  (none)")
    others = sorted(lib.load_link().get("peers") or {})
    if others:
        print(f"Other Macs linked: {', '.join(others)}   (list one with: post who <machine>; write to it as agent@project@machine)")


def source_words(info):
    """Where a session's project came from, in plain words."""
    if info.source == "declaration":
        return "declared with post join (lasts until this session exits)"
    if info.source == "marker":
        return f"from the marker file {info.where}"
    return "from the folder name"


def own_session(flag, command):
    """(me, ProjectInfo) for the session running `post join` / `post leave`. Refuses, in words, when it cannot be told which session this is:
    --as and POSTBOX_AS name no process, so they can never join a project (and it never guesses)."""
    if flag is not None:
        die(f"post {command} works on the session it runs in, and --as names no process, so nothing was changed. Run it from inside the {session_kinds()} session itself",
            reason="needs_session", result="refused")
    me = lib.my_identity()
    note(identity_method=lib.IDENTITY_TRACE.get("method"))
    if me is None or me[2] is None:
        identity_problem("cannot_tell")
        die(f"cannot tell which agent session this is, so nothing was changed. post {command} only works from inside a {session_kinds()} session "
            "(POSTBOX_AS names no process either)", reason="cannot_tell_sender", result="refused")
    info = lib.IDENTITY_PROJECT.get("info") or lib.ProjectInfo(lib.norm(me[1]))
    note(agent=me[0], sender_pid=me[2], project=info.project, source=info.source)
    return me, info


def session_kinds():
    """'Claude', 'Claude or Codex', ... for the agents whose door is on, for error sentences."""
    names = [{"claude": "Claude", "codex": "Codex", "grok": "Grok"}[n] for n in ("claude", "codex", "grok") if lib.doors.enabled(n)]
    return (", ".join(names[:-1]) + " or " + names[-1]) if len(names) > 1 else (names[0] if names else "agent")


def session_pids(me):
    """The process numbers this session is known by. A Codex screen is delivered to by its launcher process, which can differ from the
    one post found the sender at, so both carry the declaration."""
    pids = [me[2]]
    if me[0] == "codex" and lib.doors.enabled("codex"):
        screen = lib.codex_ancestor()
        if screen is not None and screen.pid not in pids:
            pids.append(screen.pid)
    return pids


def cmd_join(rest, flag):
    me, info = own_session(flag, "join")
    if not rest:
        note(op="show")
        print(f"{me[0]}@{info.project}: this session's project is {info.project}, {source_words(info)}")
        if info.note:
            print(f"note: {info.note}")
        return
    note(op="join")
    if len(rest) > 1:
        die("post join takes one project name: post join <project> (or no name, to see where this session's project comes from)", reason="bad_usage", result="refused")
    project = lib.valid_project(rest[0])
    if project is None:
        die(f"{rest[0][:40]!r} is not a project name: use letters, digits and hyphens, up to {lib.NAME_MAX}, for example myproject. Nothing was changed",
            reason="bad_project_name", result="refused")
    pids = session_pids(me)
    try:
        for pid in pids:
            lib.declare(me[0], pid, project)
    except (ValueError, OSError) as e:
        die(f"could not register this session: {e}", reason="register_failed")
    note(project=project, source="declaration", previous_project=info.project, previous_source=info.source, pids_n=len(pids))
    moved = (f" Mail for {me[0]}@{info.project} no longer reaches it: it waits, kept for {lib.ago(lib.MAX_AGE_S)}, until a session of that project is open."
             if info.project != project else "")
    print(f"joined: this {me[0]} session (process {me[2]}) now belongs to project {project}, and answers from {me[0]}@{project}.{moved} "
          "It lasts until this session exits; undo it with: post leave")


def cmd_leave(rest, flag):
    me, info = own_session(flag, "leave")
    note(op="leave")
    if rest:
        die("post leave takes no words", reason="bad_usage", result="refused")
    removed = [lib.undeclare(pid) for pid in session_pids(me)]
    lib.my_identity()   # asked again: the project is now whatever the marker file or the folder says
    now = lib.IDENTITY_PROJECT.get("info") or lib.ProjectInfo(info.project)
    note(removed=any(removed), previous_project=info.project, previous_source=info.source, project=now.project, source=now.source)
    if not any(removed):
        print(f"nothing to leave: this session has no declaration. Its project is {now.project}, {source_words(now)}")
        return
    print(f"left: this {me[0]} session is back to project {now.project}, {source_words(now)}")


def mail_headers(me, remote, inbox=None, extra=None):
    """The headers of one message. Sent-At (unix seconds) is the ORIGINAL send time: it survives forwarding, so the receiver can tell how long the mail waited.
    `extra`: From-Session (the sender's session name, so the receiver's reply command names exactly this session), To-Session (the one session
    this message is for) and Pick (the id of a held message to send on). Empty values are left out."""
    headers = {
        # the project is sent in its address form (lower case, hyphens): a folder called "My Project" must not become a reply command the shell splits
        "From": f"{me[0]}@{lib.norm(me[1])}" + (f"@{lib.my_machine()}" if remote else ""),
        "Nats-Msg-Id": uuid.uuid4().hex,
        "Sent-At": str(int(time.time())),
    }
    if inbox:
        headers["Reply-Inbox"] = inbox
    headers.update({k: str(v) for k, v in (extra or {}).items() if v})
    if not remote and me[2] is not None:   # a process number means nothing on another Mac (never sent there), and a stated --as identity has none
        headers["From-Pid"] = str(me[2])
    return headers


async def store_for_forwarding(machine, to_project, to_agent, text, me, why):
    """The other Mac cannot be reached (asleep, offline): keep the mail in THIS Mac's mailbox on forward.<machine>.<project>.<agent>.
    The relay's forwarder delivers it when that Mac answers. Always ends the command: exit 3 when stored, exit 2 when even this Mac's own mailbox is down."""
    t_pub = time.monotonic()
    try:
        nc = await lib.connect_fast(lib.local_target(), timeout=2)
        await nc.jetstream().publish(f"forward.{lib.norm(machine)}.{lib.norm(to_project)}.{to_agent}", text.encode(), headers=mail_headers(me, True, extra=MAIL_EXTRA), timeout=5)
        await nc.close()
    except Exception as e:  # noqa: BLE001
        die(f"cannot reach {machine}'s post box ({why}) and the message could NOT be stored here either ({e!r}): nothing was sent",
            reason="mailbox_down", error_class=tel.error_class(e))
    note(result="queued", reason="peer_unreachable_stored", publish_ms=tel.elapsed_ms(t_pub))
    print(f"queued: {machine} is not reachable right now; stored here and forwarded when it answers, kept for {lib.ago(lib.MAX_AGE_S)}")
    sys.exit(3)


async def send(to_agent, to_project, to_machine, text, me):
    from nats.errors import TimeoutError as NatsTimeout

    remote = to_machine is not None
    where = f" on {to_machine}" if remote else ""
    t_conn = time.monotonic()
    try:
        target = lib.peer_target(to_machine) if remote else lib.local_target()
        nc = await lib.connect_fast(target, timeout=3 if remote else 2)
    except lib.MailboxError as e:
        cls = tel.connect_error_class(e)
        note(connect_ms=tel.elapsed_ms(t_conn), error_class=cls)
        if remote:
            link_event(to_machine, "send", link_outcome(e), cls, t_conn)
            if link_outcome(e) == "unreachable":   # a refused key or a setup problem needs a person; a Mac that is asleep only needs time
                await store_for_forwarding(to_machine, to_project, to_agent, text, me, str(e)[:120])
            die(remote_problem(to_machine, e), reason="peer_" + link_outcome(e))
        if e.setup:
            die(str(e), reason="setup")   # already a complete sentence about what to fix (for example a damaged key file)
        if e.refused:
            die("the post box refused this Mac's login key. The key file changed or the post box config is old: run python3 <postbox folder>/install.py",
                reason="key_refused")
        die(f"the post box is not running (cannot reach {lib.NATS_URL}): {e}", reason="mailbox_down")
    note(connect_ms=tel.elapsed_ms(t_conn))
    if remote:
        link_event(to_machine, "send", "ok", None, t_conn)
    inbox = nc.new_inbox()
    sub = await nc.subscribe(inbox)
    js = nc.jetstream()
    headers = mail_headers(me, remote, inbox, MAIL_EXTRA)
    t_pub = time.monotonic()
    try:
        await js.publish(f"post.{lib.norm(to_project)}.{to_agent}", text.encode(), headers=headers, timeout=5)
    except Exception as e:  # noqa: BLE001
        die(f"message was NOT stored{where}: {e!r}", reason="publish_failed", error_class=tel.error_class(e), publish_ms=tel.elapsed_ms(t_pub))
    t_ack = time.monotonic()
    note(publish_ms=tel.elapsed_ms(t_pub, t_ack))
    try:
        outcome = json.loads((await sub.next_msg(timeout=8 if remote else 4)).data)
    except (NatsTimeout, Exception):  # noqa: BLE001 - a connection lost after the message was stored reads the same as no answer
        outcome = {"status": "queued", "detail": "stored, but the relay has not confirmed delivery (is the relay running?)"}
        note(reply_timeout=True)
    note(ack_ms=tel.elapsed_ms(t_ack))
    await nc.close()
    status, detail = outcome["status"], outcome["detail"]
    reason = outcome.get("reason")
    if status == "refused":
        reason = reason or tel.classify_refusal(detail)
        note(limit=tel.limit_of(reason))
    elif status == "queued":
        reason = reason or ("relay_no_answer" if CMD.get("reply_timeout") else "nobody_there")
    labels = [str(t) for t in (outcome.get("targets") or [])]
    note(result=status, reason=reason, targets_n=len(labels), target_pids=[int(n) for n in re.findall(r"\(pid (\d+)\)", " ".join(labels))],
         busy=[kind for needle, kind in (("is mid-task", "mid_task"), ("cannot be woken", "cannot_wake")) if needle in str(detail)])
    print(f"{status}: {detail}{' [on ' + to_machine + ']' if remote and status != 'held' else ''}")   # a held answer's commands already carry the machine
    sys.exit({"delivered": 0, "queued": 3, "refused": 4, "held": 5}.get(status, 2))


def split_as(argv):
    """argv without the command name -> (the --as value or None, the remaining words). Only a LEADING `--as X` / `--as=X` counts, so a
    message that happens to contain the words "--as" is never mistaken for the option."""
    rest = argv[1:]
    if rest and rest[0] == "--as":
        if len(rest) < 2:
            die('--as needs who you are: post --as claude@myproject <to> "<message>"')
        return rest[1], rest[2:]
    if rest and rest[0].startswith("--as="):
        return rest[0][len("--as="):], rest[1:]
    return None, rest


def cannot_tell():
    die("cannot tell which agent session is sending. Run this from inside an agent session, or say who you are: "
        "post --as <agent>@<project> ... (or POSTBOX_AS=<agent>@<project> post ...)", reason="cannot_tell_sender")


def watch_inbox(agent, project, interval=2.0, rounds=None, out=print):
    """Doorbell for an agent that cannot be woken (for example a monitor tool): one short line each time unread mail appears. It carries no message text (the mail is read
    with `post inbox`, which is also the only thing that clears it), so a monitor that dies or is missed loses nothing.
    Costs no AI: a local folder is listed every `interval` seconds. `rounds` limits the loop (tests); None runs until stopped."""
    announced, n = set(), 0
    while rounds is None or n < rounds:
        n += 1
        names = {p.name for p in lib.inbox_list(agent, project)}
        if names - announced:
            out(f"post box: {len(names)} unread message(s) for {agent}@{lib.norm(project)}. Run: post inbox", flush=True)
        announced = names   # a message that was read is forgotten, so mail that arrives later rings again
        if rounds is None or n < rounds:
            time.sleep(interval)


def cmd_wake_test(rest, waker=None):
    """post wake-test grokbot-desktop@project-a: one call to the agent's wake webhook with event "test". Nothing is stored in its inbox."""
    if not lib.doors.enabled("grokbot-desktop"):
        die("post wake-test needs the grokbot-desktop door, and it is switched off (see postbox/doors.py)", reason="no_door", result="refused")
    if len(rest) != 1:
        die("post wake-test takes one address: post wake-test grokbot-desktop@project-a", reason="bad_usage")
    try:
        agent, project, machine = lib.parse_target(rest[0], default_project=None)
    except ValueError as e:
        die(str(e), reason="bad_address")
    if not project:
        die("post wake-test needs the project too: post wake-test grokbot-desktop@project-a", reason="bad_address")
    if machine:
        die("post wake-test runs on the Mac that has the wake entry; leave off the @machine part", reason="bad_address")
    if lib.norm(agent) != "grokbot-desktop":
        die("post wake-test only rings the grokbot-desktop door: post wake-test grokbot-desktop@project-a", reason="bad_address", result="refused")
    waker = waker or wake.Waker()
    if not waker.configured(agent, project):
        die(f"no wake entry for {agent} in {wake.wake_file()}. Add one: " + json.dumps({agent: {"url_ref": "op://<vault>/<item>/<url-field>", "key_ref": "op://<vault>/<item>/<key-field>", "timeout_s": 5}}), code=2,
            reason="not_configured")
    result = waker.wake(agent, project, f"wake-test@{lib.my_machine()}", len(lib.inbox_list(agent, project)), event="test")
    if result.ok:
        print(f"wake: sent to {agent}@{lib.norm(project)} (HTTP {result.status})")
        return
    die(f"wake: failed for {agent}@{lib.norm(project)} ({result.reason})", reason=result.reason)


def cmd_held(rest):
    """post held: the messages kept on this Mac because they did not say which session, each with the command that sends it on."""
    if rest:
        die("post held takes no words", reason="bad_usage", result="refused")
    held = lib.hold_list()
    note(held_n=len(held))
    if not held:
        print("No held messages on this Mac.")
        return
    print(f"Held messages on this Mac ({len(held)}). Each waits because several sessions were open and it did not say which; kept {lib.ago(lib.MAX_AGE_S)} from when it was sent. "
          "Send one on without retyping it with the post pick command under it:")
    now = time.time()
    for rec in held:
        agent, project, sender = str(rec.get("agent")), str(rec.get("project")), str(rec.get("frm"))
        door = lib.DOORS.get(agent)
        try:
            open_now = door.resolve(project) if door is not None else []
        except Exception:  # noqa: BLE001 - listing must work even when a door cannot look
            open_now = []
        preview = " ".join(str(rec.get("text", "")).split())[:60]
        who = sender + (f"#{rec['session']}" if rec.get("session") else "")
        print(f"\n  {rec['id']}  to {agent}@{project}  from {who}  {lib.ago(max(0, now - float(rec.get('sent_at') or now)))} ago  \"{preview}\"")
        machine = lib.my_machine() if sender.count("@") == 2 else None   # sent from another Mac: the pick must be run THERE, naming this Mac
        if machine:
            print(f"      (sent from {sender.split('@')[2]}: run these on that Mac)")
        for cmd in lib.pick_commands(rec["id"], agent, project, open_now, machine):
            print("      " + cmd)
        if not open_now:
            print(f"      (no {agent} session is open in {project} right now)")


def cmd_inbox(rest, me):
    agent, project = me[0], lib.norm(me[1])
    if rest and rest[0] == "--watch" and len(rest) == 1:
        note(op="watch")
        emit_command(0)   # one event when the doorbell starts: it runs until stopped, and a stopped process cannot report
        try:
            watch_inbox(agent, project)
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        return
    if rest and rest[0] == "--history" and len(rest) == 1:
        history = lib.inbox_history(agent, project)
        note(op="history", messages=len(history))
        if not history:
            print(f"No messages read in the last {lib.ago(lib.MAX_AGE_S)} for {agent}@{project}.")
            return
        print(f"Messages already read by {agent}@{project} in the last {lib.ago(lib.MAX_AGE_S)}, oldest first (this does not clear anything):")
        for number, (read_at, arrived_at, path) in enumerate(history, 1):
            try:
                text = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))   # noqa: E731
            print(f"\n--- message {number} (arrived {stamp(arrived_at)}, read {stamp(read_at)}) ---\n{text}", flush=True)
        return
    if rest:
        die("post inbox takes no words (post inbox, post inbox --history, or post inbox --watch)", reason="bad_usage")
    waiting = len(lib.inbox_list(agent, project))
    note(op="take" if waiting else "empty", waiting=waiting)
    if not waiting:
        print(f"No messages waiting for {agent}@{project}.")
        return
    print(f"Post box messages for {agent}@{project}, oldest first (read once, now cleared):")
    read_bytes = [0]

    def show(number, text):
        read_bytes[0] += len(text.encode("utf-8", "replace"))
        print(f"\n--- message {number} ---\n{text}", flush=True)
    note(messages=lib.inbox_take(agent, project, show), bytes=read_bytes[0])


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(usage_text())
        sys.exit(0 if len(argv) >= 2 else 2)
    flag, args = split_as(argv)
    if not args:
        print(usage_text())
        sys.exit(2)
    if args[0] == "wake-test":   # a check of the doorbell, not mail: no sender identity needed and no telemetry event of its own
        cmd_wake_test(args[1:])
        return
    note(command=args[0] if args[0] in ("who", "whoami", "inbox", "join", "leave", "held") else "send", explicit_as=flag is not None, caller=tel.caller())
    if args[0] == "who":
        cmd_who(args[1:])
        return
    if args[0] == "held":
        cmd_held(args[1:])
        return
    if args[0] in ("join", "leave"):
        (cmd_join if args[0] == "join" else cmd_leave)(args[1:], flag)
        return
    try:
        me, how = lib.resolve_sender(flag)
    except ValueError as e:
        identity_problem("bad_identity_option")
        die(str(e), reason="bad_identity_option")
    if me is None:
        identity_problem("cannot_tell")
        cannot_tell()
    note(identity_method=lib.IDENTITY_TRACE.get("method"), from_agent=me[0], from_project=lib.norm(me[1]), sender_pid=me[2])
    if args[0] == "whoami":
        info = lib.IDENTITY_PROJECT.get("info")
        source = {"declaration": ", project declared with post join", "marker": f", project from the marker file {info.where if info else ''}"}.get(info.source if info else "", "")
        mine_name = lib.sender_session(me)
        print(f"{me[0]}@{lib.norm(me[1])}{'#' + mine_name if mine_name else ''} ({how}{', process ' + str(me[2]) if me[2] is not None else ''}{source})")
        if info and info.note:
            print(f"note: {info.note}")
        return
    if args[0] == "inbox":
        cmd_inbox(args[1:], me)
        return
    pick_id = None
    destination, rest = args[0], args[1:]
    if args[0] == "pick":   # post pick <id> <to>: send a held message on to the session named in <to>
        pick_id = args[1].strip().lower() if len(args) > 1 else ""
        if len(args) != 3 or not lib.HOLD_ID_RE.match(pick_id):
            die('post pick needs the held message\'s id and the session to send it to: post pick <id> <agent>@<project>#<name> (post held lists them)',
                reason="bad_usage", result="refused")
        destination, rest = args[2], [f"pick {pick_id}"]
        note(op="pick", pick_id=pick_id)
    try:
        to_agent, to_project, to_machine, to_session = lib.parse_destination(destination, default_project=me[1])
    except ValueError as e:
        die(str(e), reason="bad_address")
    if pick_id and not to_session:
        die(f"post pick needs the session to send it to, after a #: post pick {pick_id} {to_agent}@{lib.norm(to_project)}#<name> (post held lists the names)",
            reason="bad_address", result="refused")
    mine = f"{me[0]}@{lib.norm(me[1])}"
    note(from_addr=mine + (f"@{lib.my_machine()}" if to_machine else ""), to_addr=f"{to_agent}@{lib.norm(to_project)}" + (f"@{to_machine}" if to_machine else ""),
         to_agent=to_agent, to_project=lib.norm(to_project), to_machine=to_machine, door=to_agent, cross_machine=to_machine is not None, peer=to_machine)
    try:
        from_session = lib.sender_session(me)
    except Exception:  # noqa: BLE001 - the sender's name is a convenience: a failed look sends the message without it
        from_session = None
    note(to_session=bool(to_session), from_session=bool(from_session))
    MAIL_EXTRA.clear()
    MAIL_EXTRA.update({"From-Session": from_session, "To-Session": to_session, "Pick": pick_id})   # headers for send(): who I am by name, which session this is for
    text = sys.stdin.read() if rest == ["-"] else " ".join(rest)
    text = text.strip()
    if not text:
        die("empty message: post <to> \"what you want to say\"", reason="empty_message")
    note(via_stdin=(rest == ["-"]), **tel.text_facts(text))
    if len(text.encode()) > lib.MAX_TEXT_BYTES:
        die(f"message too long (over {lib.MAX_TEXT_BYTES} bytes); put the detail in a file and send its path", reason="too_long", limit="size")
    try:
        asyncio.run(asyncio.wait_for(send(to_agent, to_project, to_machine, text, me), 30))   # a hard limit: post must never freeze its caller
    except asyncio.TimeoutError:
        die("timed out talking to the post box (30 s); it may be stuck. Check: python3 <postbox folder>/install.py --check", reason="timeout_30s",
            error_class="TimeoutError")


def cli():
    """Entry point of the `post` command when the package is installed with pip."""
    run(sys.argv)


if __name__ == "__main__":
    run(sys.argv)
