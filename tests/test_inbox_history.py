#!/usr/bin/env python3
"""Offline tests: mail that was read stays readable for 7 days (`post inbox --history`) and never comes back in plain `post inbox`.
Run: .venv/bin/python tests/test_inbox_history.py
Everything is fake: a temporary inbox root and support folder."""
import contextlib
import io
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


def run_inbox(post_cli, rest):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        post_cli.cmd_inbox(rest, ("grokbot-desktop", "main-desk"))
    return out.getvalue()


with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    os.environ["POSTBOX_SUPPORT_DIR"] = str(td)
    os.environ["POSTBOX_INBOX_DIR"] = str(td / "inbox")
    import lib  # noqa: E402
    import post as post_cli  # noqa: E402

    t0 = 1_800_000_000.0
    print("before anything is read")
    check("history of an empty inbox says so, in words", "No messages read" in run_inbox(post_cli, ["--history"]))

    print("a message is read once, then kept")
    lib.inbox_put("grokbot-desktop", "main-desk", "first message 21sk", now=t0)
    lib.inbox_put("grokbot-desktop", "main-desk", "second message", now=t0 + 1)
    out = run_inbox(post_cli, [])
    check("plain post inbox shows and clears both", "first message 21sk" in out and "second message" in out and not lib.inbox_list("grokbot-desktop", "main-desk"))
    check("a second plain post inbox finds nothing new", "No messages waiting" in run_inbox(post_cli, []))
    hist = run_inbox(post_cli, ["--history"])
    check("history shows both drained messages, oldest first", hist.index("first message 21sk") < hist.index("second message"), hist)
    check("history says nothing is cleared and shows arrival and read times", "does not clear anything" in hist and "arrived" in hist and "read " in hist)
    check("reading history twice gives the same answer (it clears nothing)", run_inbox(post_cli, ["--history"]) == hist)
    check("history does not pull read mail back into the unread inbox", lib.inbox_list("grokbot-desktop", "main-desk") == [] and lib.inbox_depths().get("grokbot-desktop.main-desk") == 0)
    check("the archive folder and files are private (0700 / 0600)",
          oct((td / "inbox" / "grokbot-desktop.main-desk" / "read").stat().st_mode & 0o777) == "0o700"
          and all(oct(p.stat().st_mode & 0o777) == "0o600" for p in (td / "inbox" / "grokbot-desktop.main-desk" / "read").iterdir()))

    print("new mail after a read is separate")
    lib.inbox_put("grokbot-desktop", "main-desk", "third message")
    check("unread shows only the new one", len(lib.inbox_list("grokbot-desktop", "main-desk")) == 1)
    check("history still has the two old ones and not the unread one", len(lib.inbox_history("grokbot-desktop", "main-desk")) == 2 and "third message" not in run_inbox(post_cli, ["--history"]))

    print("seven days")
    now = __import__("time").time()
    check("history inside 7 days keeps the messages", len(lib.inbox_history("grokbot-desktop", "main-desk", now=now + 6 * 86400)) == 2)
    check("history after 7 days removes them", lib.inbox_history("grokbot-desktop", "main-desk", now=now + 8 * 86400) == []
          and list((td / "inbox" / "grokbot-desktop.main-desk" / "read").iterdir()) == [])
    lib.inbox_take("grokbot-desktop", "main-desk", lambda n, t: None)
    check("the relay sweep also expires the archive", lib.inbox_sweep(now=now + 9 * 86400) >= 0 and lib.inbox_history("grokbot-desktop", "main-desk", now=now + 9 * 86400) == [])

    print("other inboxes and aliases")
    lib.inbox_put("grokbot-desktop", "project-a", "project-a mail")
    lib.inbox_take("grokbot-desktop", "project-a", lambda n, t: None)
    check("each inbox has its own history", len(lib.inbox_history("grokbot-desktop", "project-a")) == 1 and lib.inbox_history("grokbot-desktop", "project-e") == [])
    (td / "aliases.json").write_text('{"project-g": "project-g-full"}')
    lib.inbox_put("grokbot-desktop", "project-g", "alias mail")
    lib.inbox_take("grokbot-desktop", "project-g-full", lambda n, t: None)
    check("history is shared by both alias names", len(lib.inbox_history("grokbot-desktop", "project-g")) == 1 == len(lib.inbox_history("grokbot-desktop", "project-g-full")))

    print("bad usage")
    try:
        run_inbox(post_cli, ["--history", "extra"])
        check("extra words after --history are refused", False)
    except SystemExit:
        check("extra words after --history are refused", True)

print(f"\n{'ALL PASSED' if FAIL == 0 else str(FAIL) + ' FAILED'}")
sys.exit(1 if FAIL else 0)
