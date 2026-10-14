"""Loaded by Python itself at start-up in every process a guarded test starts (tests/_guard.py puts this folder on PYTHONPATH and sets
PBGUARD_ACTIVE). It switches the connection guard on before any test or production code runs, so a child that wipes POSTBOX_* variables
or builds its own environment still cannot reach the real post box."""
import importlib.util
import os
import sys

if os.environ.get("PBGUARD_ACTIVE") == "1":
    _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_guard.py")
    _spec = importlib.util.spec_from_file_location("_guard_boot", _path)
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["_guard_boot"] = _module
    _spec.loader.exec_module(_module)
