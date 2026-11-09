#!/usr/bin/env python3
"""Synthetic checks for kernel-backed process identity and folder fallbacks."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import lib  # noqa: E402


def main():
    checks = []
    identity_module = lib._process_identity_module()
    original_identity = identity_module.read_process_identity
    original_cwd = identity_module.read_process_cwd
    original_run = lib._run
    try:
        identity_module.read_process_identity = lambda pid: type("I", (), {"start_sec": 123, "start_usec": 456})()
        identity_module.read_process_cwd = lambda pid: "/tmp/project-a"
        lib._run = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fallback should not run"))
        checks.append(("native process start keeps microseconds", lib.process_start(3456) == "123.000456"))
        checks.append(("native process folder avoids lsof", lib.cwd_of(3456) == "/tmp/project-a"))
        original_query = lib.codex_user_thread_snapshot
        original_age = lib.process_age_s
        original_project = lib._identity_project
        try:
            queried = []
            lib.codex_user_thread_snapshot = lambda cwds=None: (queried.append(cwds) or [])
            lib.process_age_s = lambda pid: None
            lib._identity_project = lambda agent, pid, cwd: "project-a"
            lib.codex_sessions({3456: (1, ["node", "/tool/codex"])}, project_filter="project-a")
            checks.append(("unrelated folders are excluded before thread-table reads", queried == [{"/tmp/project-a"}]))
        finally:
            lib.codex_user_thread_snapshot = original_query
            lib.process_age_s = original_age
            lib._identity_project = original_project
    finally:
        identity_module.read_process_identity = original_identity
        identity_module.read_process_cwd = original_cwd
        lib._run = original_run
    failed = sum(not passed for _, passed in checks)
    for label, passed in checks:
        print(f"  {'ok' if passed else 'FAIL'}   {label}")
    print(f"\n{len(checks)-failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
