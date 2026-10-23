# Contributing

Small, tested changes are welcome.

1. Open an issue first for anything bigger than a fix, so we agree on the shape before you spend time.
2. Set up: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. macOS (Windows is not supported), Python 3.9+ and `nats-server` (`brew install nats-server`) are needed to run every suite.
3. Every change ships with a test next to the code, in `tests/`: one case that proves it works and one that proves it fails correctly on bad input.
4. Run every suite before you open a pull request: `fail=0; for t in tests/test_*.py; do .venv/bin/python "$t" || { echo "FAILED: $t"; fail=1; }; done; test "$fail" -eq 0`. A suite that passes prints `N passed, 0 failed`, `ALL PASSED` or `OK` and exits 0; the last command exits non-zero if any suite failed. The tests need macOS with `/usr/bin/sandbox-exec` and must be run from a normal Terminal window, not inside another sandbox (an AI tool's own sandbox counts); otherwise the guard in `tests/_guard.py` stops a suite that imports it with exit code 3 (21 of the 23 suites do). A new suite must import `_env` (or `_guard`) first and start its own mailbox on a spare port, never port 4222. What the guard protects and does not cover: the README paragraph "Test guard".
5. Keep private data out of the repo: no real host names, user names, IP addresses, project names or 1Password references. Use `mac-a`, `mac-b`, `project-a` and `op://<vault>/<item>/<field>`. Check with `python3 tools/scrub_check.py`.
6. No new runtime dependencies without a reason in the pull request. The code uses the Python standard library plus `nats-py` and `nkeys`.
7. Every door (Claude, Codex, Grok, Grok Bot desktop) stays in the code and stays tested, whichever are switched on in `postbox/doors.py`. New code for a door goes behind `doors.enabled("<name>")`.

By contributing you agree that your work is released under the MIT license in `LICENSE`.
