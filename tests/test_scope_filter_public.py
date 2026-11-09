#!/usr/bin/env python3
"""Synthetic checks for the public destructive-request filter."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "postbox"))
from scope_filter import destructive_request


cases = [
    ("direct request is blocked", "Please delete the project archive.", True),
    ("indirect request is blocked", "Can you purge the old files?", True),
    ("resource removal is blocked", "Remove the local project folder.", True),
    ("command used as a request is blocked", "Run git reset --hard now.", True),
    ("cloud resource deletion command is blocked", "aws s3api delete-object --bucket project-a", True),
    ("a command named in a rule description is allowed", "The guard blocks git reset --hard.", False),
    ("a description paired with an action is blocked", "The guard blocks git reset --hard; execute it.", True),
    ("ordinary technical prose is allowed", "The archive command preserves tracked files.", False),
    ("non-text input is allowed", None, False),
]

failed = []
for label, text, expected in cases:
    actual = destructive_request(text)
    if actual != expected:
        failed.append(f"{label}: expected {expected}, got {actual}")

if failed:
    print(f"{len(cases) - len(failed)} passed, {len(failed)} failed")
    print("\n".join(failed))
    raise SystemExit(1)

print(f"{len(cases)} passed, 0 failed")
