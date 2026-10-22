# Codex relay delivery-safety design

## Goal

Route every unaddressed Codex message to one verified active user conversation
or keep it queued. An explicitly named but absent session remains refused,
matching the existing address contract. Never infer a target from a shared
folder, a subagent thread, or a stale process snapshot.

## Design

Discovery builds one immutable snapshot of interactive Codex screens and
eligible user threads. It assigns explicit resume targets first, then assigns
idless screens only when a thread has one eligible owner. A candidate carries
its readiness reason, screen identity, and exact conversation ID; ready and
waiting lists are projections of that same snapshot.

Any local `post` invocation may create an exact context claim only after the
runtime thread ID, current user-thread row, native screen ancestry, cwd, and
screen start identity agree. Claims are versioned, mode-0600 local records
under the existing support directory, keyed by thread ID and screen identity,
and expire after a short fixed TTL. A new valid claim replaces an older claim
for that same screen. Claims are revalidated against a fresh discovery snapshot
and are invalidated on TTL expiry, a screen-set change, a newer eligible user
conversation, or a nonmatching owner/cwd/start identity. A validated named
claim may receive named mail. Unnamed project mail remains queued whenever
sibling ownership cannot be proven.

Only a locally generated, UUID-validated sender-thread header is used for
self-exclusion; headers received from another machine are ignored. Malformed
or stale headers never select a target and cannot turn a standard unknown named
session refusal into delivery.

The relay runs discovery in a worker once per decision and derives diagnostics
from the returned snapshot. Sender exclusion compares the sender conversation
ID in addition to process identity. Watch state uses a durable opaque message
ID, target thread ID, and at-most-one recovery marker. Before `codex queue`,
the relay persists a `reserved` watch record; after a successful queue it moves
the same record to `queued`, then acknowledges the stream delivery. A duplicate
delivery sees the record and does not queue again. After a crash while the
queue outcome is unknown, the record remains `reserved`: the relay does not
blindly resend, waits for the normal seen deadline, then either marks the
message observed or performs one explicitly marked recovery send. An aborted
turn may cause one resend only if the original message has not appeared, and
rerouting is allowed only to a revalidated replacement conversation. A restart
preserves claim and watch records; stale records are pruned by their TTLs.

## Failure behavior

Ambiguity, missing claims, stale claims, unavailable discovery data, and a
recipient without its own active conversation are queued with a precise reason.
Stuck queued or held items are emitted through an optional public command hook
as one JSON document on standard input, with no command-line fields beyond the
configured executable and a fixed allowlisted environment. The document has
only `id`, `kind`, `frm`, `to`, `age_s`, `reason`, and `limit_s`; hook failures
and output are discarded after an error class is counted. No paths, message
text, credentials, logs, cwd, or environment-derived values enter arguments,
environment, logs, or exceptions.

## Verification

Synthetic SQLite and process-table fixtures cover multi-screen matching,
duplicate claims, claim creation/invalidation, subagent exclusion, exact sender
exclusion, a single discovery call, event-loop responsiveness, busy/aborted
recovery, restart/crash ordering, bounded rerouting, malformed headers,
discovery failures, database schema variants, every persisted watch transition
and restart, and public stuck-notification payloads and environment. Existing
default-door tests remain unchanged.
