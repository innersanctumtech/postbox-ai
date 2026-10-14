"""Post box telemetry: an OPTIONAL log of what the post box does, recorded as METADATA ONLY. Off by default; switch it on with POSTBOX_TELEMETRY=1.

How an event travels: this module writes ONE small NDJSON file per event (atomic temp-file-then-rename) into a spool folder: the folder named
by POSTBOX_TELEMETRY_SPOOL, else ~/.claude/state/spool/queue when that folder exists (a spool another tool may pick up and load somewhere),
else events are appended to a local file instead (<support dir>/telemetry-fallback.ndjson, capped at 5 MB), so nothing is silently lost and
the post box never depends on any other tool. The post box never talks to a database.

Rules this module keeps (each has a test in tests/test_telemetry.py):
  * stdlib only; imports no other tool's code.
  * Never raises, never blocks: every failure is swallowed and counted (stats()). Cost per event: well under 5 ms.
  * Switch: POSTBOX_TELEMETRY=1 turns it on; unset, 0, false, off or no leave it off. Spool folder override: POSTBOX_TELEMETRY_SPOOL.
  * PRIVACY: never the message body, never environment values, never file contents. Callers pass facts about a message (its size
    and a 12-character sha256 prefix), never the message. A key that looks like content or a secret is dropped before writing, and a
    value shaped like a login key (nkey seed) is replaced. Only the PRESENCE of a few identity-related environment variables is
    recorded (harness_hint), never their values.
  * Every event carries the host (post box machine name), the OS user and an identity ("user-host"), so two Macs that
    share one log stay distinguishable.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
COMPONENT_TYPE = "post"
SOURCE = "postbox"
FALLBACK_MAX_BYTES = 5 * 1024 * 1024
MAX_STR = 200          # longest string value kept
MAX_ITEMS = 20         # longest list kept
MAX_DEPTH = 3          # deepest nesting kept
STATUSES = ("success", "warning", "error", "blocked", "timeout")

# Keys that would carry content or secrets are never written, whatever the caller meant by them.
_FORBIDDEN_KEYS = frozenset({
    "text", "body", "message", "msg", "content", "data", "payload", "env", "environ", "environment",
    "headers", "argv", "cmdline", "command_line", "stdin", "file", "contents",
})
_FORBIDDEN_KEY_RE = re.compile(r"(secret|token|passw|seed|credential|authorization|cookie|api_?key|private)", re.I)
_SEED_RE = re.compile(r"SU[A-Z2-7]{56}")   # an nkey seed: this Mac's private login key
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_HINT_VARS = ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_PID", "CODEX_THREAD_ID", "GROK_AGENT", "POSTBOX_AS")

_STATS = {"emitted": 0, "failed": 0, "fallback": 0, "disabled": 0}
_memo: dict = {}
_last_ts = [0.0]


# ---------------------------------------------------------------- switches and where things live
def enabled() -> bool:
    return os.environ.get("POSTBOX_TELEMETRY", "0").strip().lower() in ("1", "true", "on", "yes")


def stats() -> dict:
    """Counts since this process started: emitted (written to the spool), fallback (written to the local file instead),
    failed (could not be written anywhere), disabled (skipped by the kill switch)."""
    return dict(_STATS)


def reset() -> None:
    """Forget remembered names and zero the counters (tests)."""
    _memo.clear()
    for key in _STATS:
        _STATS[key] = 0


def _norm(name) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s or "unknown"


def support_dir() -> Path:
    """Same folder lib.support_dir() names (the post box's per-Mac private files); repeated here so this module stands alone."""
    return Path(os.environ.get("POSTBOX_SUPPORT_DIR") or Path.home() / "Library" / "Application Support" / "postbox")


def machine() -> str:
    """This Mac's post box name: POSTBOX_MACHINE, else what install.py --link recorded, else the host name. Same rule as lib.my_machine()
    (tests/test_telemetry.py keeps the two equal), so an event's host is the very name used in addresses."""
    key = ("machine", os.environ.get("POSTBOX_MACHINE"), os.environ.get("POSTBOX_SUPPORT_DIR"))
    if key not in _memo:
        name = os.environ.get("POSTBOX_MACHINE")
        if not name:
            try:
                data = json.loads((support_dir() / "link.json").read_text())
                name = data.get("machine") if isinstance(data, dict) else None
            except (OSError, ValueError):
                name = None
        _memo[key] = _norm(name or socket.gethostname().split(".")[0])
    return _memo[key]


def os_user() -> str:
    if "os_user" not in _memo:
        try:
            _memo["os_user"] = _norm(getpass.getuser())
        except Exception:  # noqa: BLE001 - no passwd entry, odd sandbox
            _memo["os_user"] = "unknown"
    return _memo["os_user"]


def identity() -> str:
    """"<os user>-<machine>", for example user-mac-a."""
    return f"{os_user()}-{machine()}"


def under_test() -> bool:
    """True inside a test run (a script named test_*.py, or pytest). A test that clears the environment would otherwise lose the
    POSTBOX_TELEMETRY switch and write fake events into the REAL spool. Under test, events go only to a place the test chose itself
    (POSTBOX_TELEMETRY_SPOOL, or POSTBOX_SUPPORT_DIR for the fallback file), never to the real spool or the real support folder."""
    name = os.path.basename(sys.argv[0] or "")
    return name.startswith("test_") or "pytest" in name


def spool_dir():
    """The folder events go to, or None when the harness spool does not exist on this Mac (then the fallback file is used)."""
    forced = os.environ.get("POSTBOX_TELEMETRY_SPOOL")
    if forced:
        return Path(forced)
    if under_test():
        return None
    spool = Path.home() / ".claude" / "state" / "spool"
    return spool / "queue" if spool.is_dir() else None


def fallback_file() -> Path:
    return support_dir() / "telemetry-fallback.ndjson"


# ---------------------------------------------------------------- small facts about a command, never its content
def elapsed_ms(start, end=None):
    """Milliseconds between two time.monotonic() readings (end defaults to now), one decimal; None when start is missing."""
    if start is None:
        return None
    return round(((time.monotonic() if end is None else end) - start) * 1000.0, 1)


def text_facts(text) -> dict:
    """Facts about a message that identify it without containing it: bytes, characters and the first 12 hex characters of its sha256."""
    s = str(text)
    raw = s.encode("utf-8", "replace")
    return {"bytes": len(raw), "chars": len(s), "sha256_12": hashlib.sha256(raw).hexdigest()[:12]}


def harness_hint() -> dict:
    """Which identity-related environment variables are PRESENT (True/False), never what they hold."""
    return {name: bool(os.environ.get(name)) for name in _HINT_VARS}


def caller():
    """POSTBOX_CALLER when a script that sends mail names itself (letters, digits, hyphen, underscore, up to 32), else None."""
    value = os.environ.get("POSTBOX_CALLER", "").strip().lower()
    return value if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", value) else None


def error_class(exc) -> str:
    """The exception's class name only. Its text can hold anything, so it is never recorded."""
    return type(exc).__name__ if isinstance(exc, BaseException) else "unknown"


def error_class_of_text(text) -> str:
    """The leading class name of a repr such as "NoServersError('...')", else 'message'. For relay_error lines, which carry text."""
    m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.]{0,60})\(", str(text))
    return m.group(1) if m else "message"


def connect_error_class(exc) -> str:
    """Why a mailbox could not be used, in a fixed vocabulary. `exc` is a lib.MailboxError (flags refused/setup) or any exception."""
    text = f"{type(exc).__name__} {exc}"
    if getattr(exc, "setup", False):
        for needle, name in (("no other Mac called", "peer_not_linked"), ("no login key yet", "no_login_key"),
                             ("cannot find", "peer_dns"), ("damaged", "key_damaged"), ("cannot read this Mac's login key", "key_unreadable")):
            if needle in text:
                return name
        return "setup"
    if getattr(exc, "refused", False):
        return "auth_refused"
    for needle, name in (("NoServersError", "no_servers"), ("Timeout", "timeout"), ("timed out", "timeout"),
                         ("Connect call failed", "conn_refused"), ("Connection refused", "conn_refused"),
                         ("uthorization", "auth_refused"), ("uthentication", "auth_refused")):
        if needle in text:
            return name
    return "other"


def classify_refusal(detail) -> str:
    """Reason class of a refusal from the relay's answer, for an older relay that sends no reason of its own."""
    d = str(detail)
    if "message too long" in d:
        return "too_long"
    if "stop rule" in d:
        return "recipient_limit" if "all senders together" in d else "pair_limit"
    if "no delivery door" in d:
        return "no_door"
    return "other"


def limit_of(reason):
    """pair / recipient / size when the reason is one of the stop rules or caps, else None."""
    return {"pair_limit": "pair", "recipient_limit": "recipient", "too_long": "size", "inbox_full": "inbox", "bad_reply_inbox": "reply_inbox"}.get(reason)


def status_for(result) -> str:
    """telemetry_events.status for a result: delivered/ok -> success, queued/held -> warning, refused -> blocked, anything else -> error."""
    return {"delivered": "success", "ok": "success", "queued": "warning", "held": "warning", "refused": "blocked", "timeout": "timeout"}.get(str(result), "error")


# ---------------------------------------------------------------- cleaning: the privacy gate
def _clean(value, depth=0):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else round(value, 3)
    if isinstance(value, str):
        text = _SEED_RE.sub("[redacted]", _CONTROL_RE.sub(" ", value))
        return text if len(text) <= MAX_STR else text[:MAX_STR] + "..."
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:MAX_ITEMS * 2]:
            key = str(k)[:40]
            if key.lower() in _FORBIDDEN_KEYS or _FORBIDDEN_KEY_RE.search(key):
                continue
            out[key] = _clean(v, depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        return [_clean(v, depth + 1) for v in items[:MAX_ITEMS]]
    return type(value).__name__   # an object nobody asked us to describe: its class name, never its repr


def _unique_ts() -> datetime:
    """Now, with microseconds, never equal to the last one this process used: the table's unique key includes the timestamp."""
    now = time.time()
    if now <= _last_ts[0]:
        now = _last_ts[0] + 1e-6
    _last_ts[0] = now
    return datetime.fromtimestamp(now, tz=timezone.utc)


# ---------------------------------------------------------------- writing
def _write_spool(folder: Path, event_id: str, line: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".tmp.post-{os.getpid()}-{event_id[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(line)
        os.replace(tmp, folder / f"post-{int(time.time() * 1000)}-{event_id[:8]}.ndjson")
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_fallback(line: str) -> bool:
    if under_test() and not os.environ.get("POSTBOX_SUPPORT_DIR"):
        return False   # a test that did not point the support folder at a temp dir must not write into the real one
    path = fallback_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > FALLBACK_MAX_BYTES:
            return False
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(line)
    return True


def build_event(event_type, component_name, status="success", duration_ms=None, error_message=None, session_id=None, **fields) -> dict:
    """The spool envelope for one event (what emit writes). Exposed so tests can inspect exactly what would be stored."""
    event_id = str(uuid.uuid4())
    host = machine()
    meta = {"schema": SCHEMA, "host": host, "os_user": os_user(), "identity": identity(), "pid": os.getpid(),
            "via": os.path.basename(sys.argv[0] or "") or "unknown"}
    meta.update(_clean(fields))
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    dur = _clean(duration_ms) if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) else None
    return {
        "event_id": event_id,
        "event_type": str(event_type),
        "session_id": session_id or f"postbox-{host}-{os.getpid()}",
        "project_path": cwd,
        "ts": _unique_ts().isoformat(),
        "payload": {
            "component_type": COMPONENT_TYPE,
            "component_name": str(component_name),
            "status": status if status in STATUSES else "error",
            "duration_ms": dur,
            "error_message": _clean(error_message) if error_message else None,
            "source": SOURCE,
            "metadata": meta,
        },
    }


def emit(event_type, component_name, status="success", duration_ms=None, error_message=None, session_id=None, **fields) -> bool:
    """Record one event. Returns True when it reached the spool or the fallback file, False otherwise. NEVER raises.

    `fields` are the event's metadata: facts only (counts, names of addresses, timings, result classes). Anything that looks like
    content or a secret is dropped by key, and any string is cut to 200 characters."""
    if not enabled():
        _STATS["disabled"] += 1
        return False
    try:
        event = build_event(event_type, component_name, status, duration_ms, error_message, session_id, **fields)
        line = json.dumps(event, default=str, separators=(",", ":")) + "\n"
    except Exception:  # noqa: BLE001
        _STATS["failed"] += 1
        return False
    try:
        folder = spool_dir()
        if folder is not None:
            _write_spool(folder, event["event_id"], line)
            _STATS["emitted"] += 1
            return True
    except Exception:  # noqa: BLE001 - an unusable spool folder falls through to the local file
        pass
    try:
        if _write_fallback(line):
            _STATS["fallback"] += 1
            return True
    except Exception:  # noqa: BLE001
        pass
    _STATS["failed"] += 1
    return False
