#!/usr/bin/env python3
"""Offline safety checks for local Codex context claims.

The fixtures below replace process and thread discovery.  Nothing reads a real
Codex state database, process table, or support directory.
"""
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["POSTBOX_SUPPORT_DIR"] = tempfile.mkdtemp(prefix="postbox-claims-")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import lib  # noqa: E402
import post  # noqa: E402

PASS = FAIL = 0
THREAD = "01a1aaaa-0000-7000-8000-000000000001"
NEWER = "01a1aaaa-0000-7000-8000-000000000002"
CWD = "/workspace/project-a"
NOW = time.time()


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


public_sources = list((ROOT / "postbox").glob("*.py"))
pep604 = [source.name for source in public_sources if re.search(r"->\s*[^#\n]*\s\|\s", source.read_text())]
check("public Python source avoids PEP 604 annotations for Python 3.9", not pep604, pep604)


screen = lib.Session("codex", 101, CWD, "project-a", THREAD, True)
snapshot = [{"pid": 101, "start": "2026-10-23 12:00:00"}]
context = {"id": THREAD, "cwd": CWD, "created_at": NOW - 20, "updated_at": NOW, "source": "user"}


def claimed(screens=snapshot, sessions=None, row=context, users=None):
    sessions = [screen] if sessions is None else sessions
    users = [(THREAD, CWD, NOW, NOW - 20)] if users is None else users
    with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD}, clear=False), \
            mock.patch.object(lib, "codex_context_screen_snapshot", return_value=screens), \
            mock.patch.object(lib, "codex_sessions", return_value=sessions), \
            mock.patch.object(lib, "codex_thread_context", return_value=row), \
            mock.patch.object(lib, "codex_user_thread_snapshot", return_value=users):
        return lib.codex_claim_self(now=NOW), lib.thread_context_claim_read(THREAD, now=NOW, sessions=sessions, snapshot=screens)


claim, read = claimed()
path = lib.support_dir() / "codex-claims" / f"{THREAD}.json"
check("a valid local user thread claims its matching native screen", claim is screen and read is screen, (claim, read))
check("claims are private, versioned TTL records", path.exists() and (path.stat().st_mode & 0o777) == 0o600
      and '"version":1' in path.read_text() and '"expires_at"' in path.read_text())

changed = [{"pid": 101, "start": "different-start"}]
with mock.patch.object(lib, "codex_thread_context", return_value=context), \
        mock.patch.object(lib, "codex_user_thread_snapshot", return_value=[(THREAD, CWD, NOW, NOW - 20)]), \
        mock.patch.object(lib, "codex_sessions", return_value=[screen]):
    check("a screen-set or owner-start change invalidates a claim", lib.thread_context_claim_read(THREAD, now=NOW, sessions=[screen], snapshot=changed) is None)
    newer = [(NEWER, CWD, NOW, NOW + 1), (THREAD, CWD, NOW, NOW - 20)]
    with mock.patch.object(lib, "codex_user_thread_snapshot", return_value=newer):
        check("a newer user conversation invalidates the old claim", lib.thread_context_claim_read(THREAD, now=NOW, sessions=[screen], snapshot=snapshot) is None)
    moved = dict(context, cwd="/workspace/other")
    with mock.patch.object(lib, "codex_thread_context", return_value=moved):
        check("a thread cwd mismatch invalidates a claim", lib.thread_context_claim_read(THREAD, now=NOW, sessions=[screen], snapshot=snapshot) is None)

with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD}, clear=False), \
        mock.patch.object(lib, "codex_thread_context", return_value=dict(context, source="subagent")), \
        mock.patch.object(lib, "codex_context_screen_snapshot", return_value=snapshot), \
        mock.patch.object(lib, "codex_sessions", return_value=[screen]):
    check("subagent threads cannot create claims", lib.codex_claim_self(now=NOW) is None)

local = post.mail_headers(("codex", "project-a", 101), False, extra={"From-Thread": THREAD})
remote = post.mail_headers(("codex", "project-a", 101), True, extra={"From-Thread": THREAD})
check("only local mail carries an exact sender-thread header", local.get("From-Thread") == THREAD and "From-Thread" not in remote, (local, remote))

other = lib.Session("codex", 202, CWD, "project-a", NEWER, True)
with mock.patch.object(lib, "thread_context_claims", return_value=[]):
    selected = lib.CodexDoor().resolve("project-a", sessions=[screen, other], exclude_thread=THREAD)
check("sender exclusion removes only the exact UUID target", [s.target for s in selected] == [NEWER], selected)

with mock.patch.object(lib.os, "getpid", return_value=900), \
        mock.patch.object(lib, "_run", return_value="1 /bin/python3\n"), \
        mock.patch.object(lib, "codex_claim_self", return_value=screen):
    check("shared app-server ancestry uses a claim only after matching screen proof", lib.native_sender_identity(THREAD) == ("codex", "project-a", 101))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
