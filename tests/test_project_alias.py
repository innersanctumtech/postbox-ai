#!/usr/bin/env python3
"""Offline tests: aliases.json makes two project names ONE project, in both directions (project-g <-> project-g-full).
Run: .venv/bin/python tests/test_project_alias.py
Everything is fake: a temporary support folder (POSTBOX_SUPPORT_DIR) and a temporary inbox root. No real session or mailbox is touched."""
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
import sys
import tempfile
from pathlib import Path

os.environ["POSTBOX_TELEMETRY"] = "0"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "postbox"))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)

FAIL = 0


def check(label, cond, detail=""):
    global FAIL
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))
    FAIL += 0 if cond else 1


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    os.environ["POSTBOX_SUPPORT_DIR"] = str(td)
    os.environ["POSTBOX_INBOX_DIR"] = str(td / "inbox")
    import lib  # noqa: E402
    import wake  # noqa: E402

    print("no aliases.json: nothing changes")
    check("canon_project is just norm", lib.canon_project("Project G Full") == "project-g-full" and lib.canon_project("project-g") == "project-g")
    check("the two names are different projects", not lib.same_project("project-g", "project-g-full"))
    check("inbox folders differ", lib.inbox_dir("grokbot-desktop", "project-g") != lib.inbox_dir("grokbot-desktop", "project-g-full"))

    (td / "aliases.json").write_text(json.dumps({"project-g": "project-g-full"}))
    print("alias project-g -> project-g-full (both names are one project)")
    check("canon: project-g becomes project-g-full, project-g-full stays", lib.canon_project("project-g") == "project-g-full" == lib.canon_project("project-g-full"))
    check("same_project is true in both directions", lib.same_project("project-g", "project-g-full") and lib.same_project("project-g-full", "project-g"))
    check("an unrelated project is untouched", lib.canon_project("project-a") == "project-a" and not lib.same_project("project-a", "project-g"))
    check("one inbox folder for both names", lib.inbox_dir("grokbot-desktop", "project-g") == lib.inbox_dir("grokbot-desktop", "project-g-full"))
    check("the inbox folder name is the shown (right-hand) name", lib.inbox_dir("grokbot-desktop", "project-g").name == "grokbot-desktop.project-g-full")

    s_short = lib.Session("claude", 11, "/x/project-g", "project-g", "sock")
    s_long = lib.Session("claude", 12, "/x/project-g-full", "project-g-full", "sock")
    door = lib.ClaudeDoor()
    check("mail to claude@project-g-full reaches the session whose project is project-g", door.resolve("project-g-full", sessions=[s_short]) == [s_short])
    check("mail to claude@project-g reaches the session whose project is project-g-full (vice versa)", door.resolve("project-g", sessions=[s_long]) == [s_long])
    check("both sessions are found whichever name is typed", len(door.resolve("project-g", sessions=[s_short, s_long])) == 2)
    check("a session in another project is not matched", door.resolve("project-g", sessions=[lib.Session("claude", 13, "/x/p", "project-a", "s")]) == [])
    check("exclude_pid still works", door.resolve("project-g", exclude_pid=11, sessions=[s_short, s_long]) == [s_long])
    inbox_door = lib.DOORS["grokbot-desktop"]
    check("the inbox-only Desk door resolves both names to the same project", inbox_door.resolve("project-g")[0].project == inbox_door.resolve("project-g-full")[0].project == "project-g-full")

    print("wake entries follow the alias")
    check("wake keys are equal for both names", wake.wake_key("grokbot-desktop", "project-g") == wake.wake_key("grokbot-desktop", "project-g-full") == "grokbot-desktop@project-g-full")
    entry = {"url_ref": "op://<vault>/<item>/url", "header_ref": "op://<vault>/<item>/header"}
    for typed in ("project-g", "project-g-full"):
        (td / "wake.json").write_text(json.dumps({f"grokbot-desktop@{typed}": entry}))
        w = wake.Waker()
        check(f"a wake.json key typed as {typed!r} is found for both names",
              w.has_own_entry("grokbot-desktop", "project-g") and w.has_own_entry("grokbot-desktop", "project-g-full"), str(wake.load_wake().keys()))

    print("bad alias files never break anything")
    for bad in ("not json", "[1,2]", json.dumps({"a": 5}), json.dumps({"a": "b", "b": "a"})):
        (td / "aliases.json").write_text(bad)
        r = lib.canon_project("a")
        check(f"{bad[:20]!r}: canon_project returns a name and does not hang", isinstance(r, str) and r in ("a", "b"), r)
    (td / "aliases.json").unlink()
    check("a missing file means no aliases", lib.project_aliases() == {})

print(f"\n{'ALL PASSED' if FAIL == 0 else str(FAIL) + ' FAILED'}")
sys.exit(1 if FAIL else 0)
