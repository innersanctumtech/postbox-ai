#!/usr/bin/env python3
"""Offline tests: a Codex screen that started a NEW conversation in the same window (/new) gets its mail in that new conversation.
Run: .venv/bin/python tests/test_codex_new_conversation.py

Requirement: a Codex screen launched as `codex resume <old>` that then starts a new conversation must receive its mail in the new one.
The box must not keep taking the conversation from the launch arguments, or mail lands in the abandoned conversation and Codex never sees it.
Also covered: a subagent's thread (Codex records those as thread_source='subagent') is never anyone's mailbox.
The thread table is a throwaway sqlite file; nothing here touches ~/.codex or a real process.
"""
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["POSTBOX_MACHINE"] = "mac-a"
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import lib  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


NOW = time.time()
CWD, OTHER = "/home/u/apps/final_project-b", "/home/u/apps/project-a"
OLD = "01a0d8a4-0000-7000-8000-000000000001"
NEW = "01a130ef-0000-7000-8000-000000000002"
SUB = "01a130f1-0000-7000-8000-000000000003"
NEW2 = "01a13200-0000-7000-8000-000000000004"
tmp = tempfile.mkdtemp(prefix="postbox-codex-new-")
DB = os.path.join(tmp, "state_5.sqlite")
OLD_DB = os.path.join(tmp, "state_4.sqlite")


def make(path, modern=True):
    con = sqlite3.connect(path)
    cols = "id text primary key, cwd text, archived integer, updated_at integer, created_at integer, name text"
    con.execute(f"create table threads ({cols}{', thread_source text' if modern else ''})")
    con.commit()
    return con


con = make(DB)
lib.codex_state_db = lambda: DB


def put(tid, cwd, created, updated, source="user", c=None, archived=0):
    c = c or con
    if source is None:
        c.execute("insert or replace into threads (id, cwd, archived, updated_at, created_at) values (?,?,?,?,?)", (tid, cwd, archived, int(updated), int(created)))
    else:
        c.execute("insert or replace into threads (id, cwd, archived, updated_at, created_at, thread_source) values (?,?,?,?,?,?)",
                  (tid, cwd, archived, int(updated), int(created), source))
    c.commit()


def clear(c=None):
    (c or con).execute("delete from threads")
    (c or con).commit()


AGE = {100: 10 * 86400.0, 200: 600.0}     # screen 100 opened 10 days ago, screen 200 ten minutes ago
CWDS = {100: CWD, 200: CWD}
lib.process_age_s = lambda pid: AGE.get(pid)
lib.cwd_of = lambda pid: CWDS.get(pid)
RESUMED = {100: (1, ["node", "/x/bin/codex", "resume", OLD])}
DAY = 86400


def target(sessions, pid=100):
    s = [x for x in sessions if x.pid == pid]
    return (s[0].target, s[0].ready) if s else (None, None)


print("-- (a) regression: resumed screen, then a new conversation in the same window")
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 3 * 3600)
put(NEW, CWD, NOW - 3 * 3600 + 30, NOW - 60)
t, ready = target(lib.codex_sessions(RESUMED))
check("mail goes to the new conversation, not the abandoned one", t == NEW and ready, (t, ready))

print("-- (b) a subagent's thread is never the screen's mailbox")
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 120)
put(SUB, CWD, NOW - 3600, NOW - 30, source="subagent")
t, ready = target(lib.codex_sessions(RESUMED))
check("resumed screen with only a newer subagent thread keeps its own", t == OLD and ready, (t, ready))
PLAIN = {200: (1, ["node", "/x/bin/codex"])}
clear()
put(NEW, CWD, NOW - 500, NOW - 200)
put(SUB, CWD, NOW - 400, NOW - 10, source="subagent")
t, ready = target(lib.codex_sessions(PLAIN), pid=200)
check("plain screen picks its own conversation, not the newer subagent thread", t == NEW and ready, (t, ready))

print("-- (c) no new conversation: unchanged")
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 60)
t, ready = target(lib.codex_sessions(RESUMED))
check("resumed screen keeps the launch conversation", t == OLD and ready, (t, ready))
put(NEW, OTHER, NOW - 3600, NOW - 30)
t, ready = target(lib.codex_sessions(RESUMED))
check("a new conversation in ANOTHER folder is not adopted", t == OLD and ready, (t, ready))
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 60)
put(NEW, CWD, NOW - 20 * DAY, NOW - 15 * DAY)
t, ready = target(lib.codex_sessions(RESUMED))
check("a conversation created before the screen opened is not adopted", t == OLD and ready, (t, ready))

print("-- (d) another screen owns the new conversation by its launch arguments")
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 3600)
put(NEW, CWD, NOW - 300, NOW - 30)
BOTH = {100: (1, ["node", "/x/bin/codex", "resume", OLD]), 200: (1, ["node", "/x/bin/codex", "resume", NEW])}
ss = lib.codex_sessions(BOTH)
check("screen 100 keeps its own", target(ss, 100) == (OLD, True), target(ss, 100))
check("screen 200 keeps its own", target(ss, 200) == (NEW, True), target(ss, 200))

print("-- (e) two screens in one folder and a new conversation either could own: never guess")
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 3600)
put(NEW2, CWD, NOW - 200, NOW - 20)
AMBIG = {100: (1, ["node", "/x/bin/codex", "resume", OLD]), 200: (1, ["node", "/x/bin/codex"])}
ss = lib.codex_sessions(AMBIG)
check("the resumed screen is listed but not a delivery target (mail waits, not lost)", target(ss, 100) == ("", False), target(ss, 100))

print("-- (f) an explicit resume target is reserved before matching an idless screen")
clear()
put(NEW, CWD, NOW - 300, NOW - 30)
RESERVED = {100: (1, ["node", "/x/bin/codex"]), 200: (1, ["node", "/x/bin/codex", "resume", NEW])}
ss = lib.codex_sessions(RESERVED)
check("the idless screen cannot steal the explicitly resumed conversation", target(ss, 100) == ("", False), target(ss, 100))
check("the resumed screen keeps its explicit target", target(ss, 200) == (NEW, True), target(ss, 200))

print("-- (g) an explicit resume target can be archived")
clear()
put(OLD, CWD, NOW - 17 * DAY, NOW - 60, archived=1)
t, ready = target(lib.codex_sessions(RESUMED))
check("an archived explicitly resumed conversation remains ready", t == OLD and ready, (t, ready))

print("-- (h) idless sibling screens competing for one candidate remain unready")
clear()
put(NEW, CWD, NOW - 300, NOW - 30)
SIBLINGS = {100: (1, ["node", "/x/bin/codex"]), 200: (1, ["node", "/x/bin/codex"])}
ss = lib.codex_sessions(SIBLINGS)
check("the first sibling is unready rather than guessing", target(ss, 100) == ("", False), target(ss, 100))
check("the second sibling is unready rather than guessing", target(ss, 200) == ("", False), target(ss, 200))

print("-- (i) sequential idless screens receive their uniquely time-compatible conversations")
clear()
AGE.update({300: 900.0, 400: 600.0})
CWDS.update({300: CWD, 400: CWD})
put(NEW, CWD, NOW - 800, NOW - 700)
put(NEW2, CWD, NOW - 500, NOW - 30)
SEQUENTIAL = {300: (1, ["node", "/x/bin/codex"]), 400: (1, ["node", "/x/bin/codex"])}
ss = lib.codex_sessions(SEQUENTIAL)
check("the earlier screen receives only its compatible conversation", target(ss, 300) == (NEW, True), target(ss, 300))
check("the later screen receives its newer conversation", target(ss, 400) == (NEW2, True), target(ss, 400))

print("-- (j) an older Codex whose table has no thread_source: old behaviour")
old_con = make(OLD_DB, modern=False)
lib.codex_state_db = lambda: OLD_DB
put(OLD, CWD, NOW - 17 * DAY, NOW - 3600, source=None, c=old_con)
put(NEW, CWD, NOW - 300, NOW - 30, source=None, c=old_con)
t, ready = target(lib.codex_sessions(RESUMED))
check("without thread_source the launch conversation is kept", t == OLD and ready, (t, ready))
lib.codex_state_db = lambda: DB

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
