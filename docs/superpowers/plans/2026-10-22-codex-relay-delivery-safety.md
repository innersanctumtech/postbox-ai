# Codex Relay Delivery Safety Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Codex delivery choose a proven active conversation or queue safely, while preserving public defaults and metadata-only diagnostics.

**Architecture:** `postbox/lib.py` owns immutable screen/thread discovery, unique matching, and claim validation. `postbox/relay.py` consumes one worker-built candidate snapshot per decision and manages bounded watch recovery and notifications. Synthetic tests inject process tables, SQLite state, clocks, and doors.

**Tech Stack:** Python standard library, asyncio, sqlite3, existing offline test harness.

---

### Task 1: Snapshot and unique matching

**Files:**
- Modify: `postbox/lib.py`
- Modify: `tests/test_codex_thread_ready.py`
- Modify: `tests/test_codex_new_conversation.py`

- [ ] Add failing fixtures for two resumed screens, idless siblings, and subagent rows.
- [ ] Run the focused offline test scripts and confirm the old independent newest-thread selection fails the new expectations.
- [ ] Implement one raw-screen snapshot and a unique idless matcher: reserve explicit targets, ignore subagents, and leave ambiguity unready.
- [ ] Re-run both focused scripts; commit the matching unit.

### Task 2: Exact claims and sender exclusion

**Files:**
- Modify: `postbox/lib.py`
- Modify: `postbox/post.py`
- Modify: `postbox/relay.py`
- Create: `tests/test_codex_claims.py`

- [ ] Add failing synthetic fixtures for valid, duplicate, stale, and changed-screen claims; malformed local and cross-machine sender-thread headers; and sender conversation exclusion.
- [ ] Implement versioned, mode-0600, TTL-bounded claim storage in the existing support area; bind each record to a screen PID/start/cwd and replace only that screen's older claim.
- [ ] Implement snapshot revalidation, local-only sender-thread headers, and exact target exclusion. Preserve refusal for an explicitly named but absent session.
- [ ] Run claim and existing session-addressing tests; commit the claim unit.

### Task 3: One async candidate decision

**Files:**
- Modify: `postbox/lib.py`
- Modify: `postbox/relay.py`
- Modify: `tests/test_codex_thread_ready.py`

- [ ] Add a failing fixture asserting ready/waiting projections use one discovery call and an event-loop callback proceeds while discovery blocks.
- [ ] Add `CodexDoor.candidates()` returning ready and waiting candidates from one snapshot.
- [ ] Replace relay's separate resolve/waiting calls with one `asyncio.to_thread` candidate call and derive diagnostics only from its metadata.
- [ ] Run the focused async regression; commit the discovery unit.

### Task 4: Watch recovery and public stuck notifications

**Files:**
- Modify: `postbox/lib.py`
- Modify: `postbox/relay.py`
- Modify: `codex-skill/post/SKILL.md`
- Modify: `claude-skill/post/SKILL.md`
- Create: `tests/test_stuck_mail_and_claims.py`

- [ ] Add failing synthetic cases for busy waiting, one aborted-turn resend, safe reroute, `reserved`/`queued` watch transitions, restart at every transition, a crash after queue acceptance with unknown outcome, stuck claim-not-ready, and metadata-only notification fields.
- [ ] Implement stable watch identity with durable `reserved` then `queued` transitions; reserve before calling the door, never duplicate a reservation, and use one marked deadline recovery when queue acceptance was unknowable. Keep bounded retry/reroute and TTL cleanup.
- [ ] Implement the notifier as stdin-only JSON with exactly the public fields and a fixed allowlisted environment; discard hook output and assert paths, text, cwd, inherited environment values, and exceptions cannot leak.
- [ ] Update both shipped skills to distinguish queued mail, received mail, claims, and ambiguity holds without exposing local state.
- [ ] Run the new test and affected existing scripts, including discovery failure and old/new thread-table schema variants; commit the recovery unit.

### Task 5: Integration verification and public-surface review

**Files:**
- Modify: affected files only
- Test: `tests/test_codex_thread_ready.py`, `tests/test_codex_new_conversation.py`, `tests/test_codex_seen_check.py`, `tests/test_session_addressing.py`, `tests/test_stuck_mail_and_claims.py`

- [ ] Name the acceptance gate: all synthetic routing and recovery cases must pass with public defaults unchanged.
- [ ] Run focused tests once against the finalized source.
- [ ] Run `python3 tools/scrub_check.py` to confirm public-source hygiene.
- [ ] Review the diff for paths, logs, messages, credentials, and changed default doors; commit final integration fixes only if required.
- [ ] After successful local verification, commit the completed work. Push only as the separately authorized owner-controlled publication action, then verify the remote commit IDs before reporting it.
