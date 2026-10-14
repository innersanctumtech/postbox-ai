"""Imported first by the suites that need every door on (19 of the 21 suites import the guard, most of them through this file): switches EVERY delivery door on for that test process.

The public default is Claude only (postbox/doors.py), but the Codex, Grok and Grok Bot desktop doors stay in the code and stay tested,
so these suites need them on. tests/test_doors.py is the one suite that checks the shipped default.
Suites that start the installer or the CLI as a child process pass ALL_DOORS_ENV as POSTBOX_DOORS themselves.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _guard  # noqa: E402,F401  (exits when the default post box address is in use; points tests at a dead port otherwise)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_ROOT, "postbox") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "postbox"))
os.environ.pop("POSTBOX_DOORS", None)      # a stray value in the caller's shell must not change what the suites check
import doors  # noqa: E402

doors.ENABLED_DOORS.update(doors.ALL_DOORS)   # the module list, not the environment: several suites wipe POSTBOX_* variables after importing
ALL_DOORS_ENV = ",".join(doors.ALL_DOORS)
