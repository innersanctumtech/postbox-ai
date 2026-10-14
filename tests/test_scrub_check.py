#!/usr/bin/env python3
"""tools/scrub_check.py: a clean tree passes, and each kind of planted private string fails with the right rule.

Every private string below is assembled at run time, so this file itself stays clean for the gate that scans it.
Run: python3 tests/test_scrub_check.py
"""
import os
os.environ["POSTBOX_TELEMETRY"] = "0"
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "scrub_check.py"
sys.path.insert(0, str(ROOT / "tools"))
sys.dont_write_bytecode = True   # before the import below: importing tools/scrub_check.py would otherwise write tools/__pycache__
import scrub_check  # noqa: E402

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def run(root, *args):
    r = subprocess.run([sys.executable, "-B", str(TOOL), "--root", str(root), *args], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def tree(base, files):
    for name, text in files.items():
        path = Path(base, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(text)
    return base


CLEAN = {"README.md": "# demo\nrun `post claude@project-a@mac-b`\nlisten on 127.0.0.1:4222\n",
         "postbox/x.py": 'REF = "op://<vault>/<item>/<field>"\nok = ref.startswith("op://")\nv = "1.10.0.1"\n'}
HOME = "/" + "Users" + "/someone/project"
SEED = "SU" + "A" * 56
PLANTS = [
    ("a home folder path", f"cd {HOME}\n", "home-path"),
    ("a 10.x address", "host = '10." + "1.2.3'\n", "private-ip"),
    ("a class C private address", "ssh 192." + "168.0.20\n", "private-ip"),
    ("a 172.16.x address", "ip 172." + "16.5.5\n", "private-ip"),
    ("a 172.31.x address", "ip 172." + "31.5.5\n", "private-ip"),
    ("a link-local 169.254.x address", "ip 169." + "254.1.1\n", "private-ip"),
    ("a Tailscale 100.64.x address", "ip 100." + "64.1.1\n", "private-ip"),
    ("a Tailscale 100.127.x address", "ip 100." + "127.1.1\n", "private-ip"),
    ("a real tailnet machine name", "host mac-b.tail" + "957fa4.ts.net\n", "tailnet-name"),
    ("a real 1Password reference", "ref = 'op:" + "//Vault/Item/field'\n", "op-reference"),
    ("a login seed", f"seed = {SEED}\n", "login-seed"),
    ("a placeholder vault with a real item", "ref = 'op:" + "//<vault>/Real Item/field'\n", "op-reference"),
    ("a real vault with a placeholder item", "ref = 'op:" + "//Vault/<item>/field'\n", "op-reference"),
    ("a dated comment", "# fixed after the " + "2026" + "-10-12 problem\n", "dated-note"),
    ("a date glued to a word with a hyphen", "# see release-" + "2026" + "-10-12\n", "dated-note"),
    ("a 10.x address at the end of a sentence", "connect to 10." + "1.2.3.\n", "private-ip"),
    ("a 10.x address followed by a comma", "hosts: 10." + "1.2.3, other\n", "private-ip"),
    ("a login seed glued to a word with an underscore", f"key_{SEED}\n", "login-seed"),
    ("a placeholder vault with a real word glued to the placeholder item", "ref = 'op:" + "//<vault>/<item>Real/field'\n", "op-reference"),
    ("placeholder vault and item with a real field name", "ref = 'op:" + "//<vault>/<item>/RealField'\n", "op-reference"),
    ("a placeholder vault with a real item and a generic field", "ref = 'op:" + "//<vault>/ActualItem/password'\n", "op-reference"),
    ("a placeholder section followed by a real field", "ref = 'op:" + "//<vault>/<item>/<section>/RealField'\n", "op-reference"),
    ("a reference that starts with a dot", "ref = 'op:" + "//.hidden/item/field'\n", "op-reference"),
]

print("clean tree")
with tempfile.TemporaryDirectory() as tmp:
    repo, deny = Path(tmp, "repo"), Path(tmp, "deny.txt")
    deny.write_text("# comment\n\nsecretname\n")
    code, out = run(tree(repo, CLEAN))
    check("a clean tree passes with exit 0 and prints 0 hits", code == 0 and out.strip().endswith("scrub gate: 0 hits"), out)
    code, out = run(repo, "--denylist", str(deny))
    check("a clean tree also passes with a denylist that matches nothing", code == 0 and "0 hits" in out, out)

print("planted strings, one at a time")
for label, text, rule in PLANTS:
    with tempfile.TemporaryDirectory() as tmp:
        tree(tmp, {**CLEAN, "docs/notes.md": text})
        code, out = run(tmp)
        check(f"{label} fails with rule {rule} and names the file and line", code == 1 and f"docs/notes.md:1: [{rule}]" in out and "scrub gate: 1 hit" in out, out)

print("things that must NOT be flagged")
SAFE = ["ip 172." + "32.0.1\n", "ip 100." + "128.0.1.1\n", "ip 100." + "63.0.1\n", "ip " + ".".join(["11", "10", "0", "1", "2"]) + "\n", "ip " + ".".join(["1", "10", "0", "1"]) + "\n", "bind 127.0.0.1\n",
        "ref op:" + "//<vault>/<item>/<field>\n", "ref op:" + "//<vault>/<item>/url\n", "data " + "2026" + "-09-30T23:15:19Z\n", "starts with op:" + "//\n", "see op:" + "//...\n", "public key U" + "A" * 55 + "\n", "SU is a prefix\n",
        "ver " + ".".join(["10", "1", "2", "3", "4"]) + "\n", "ver " + ".".join(["172", "16", "0", "1", "5"]) + "\n", "ref op:" + "//<vault>/<item>/url?attribute=otp\n", "ref op:" + "//<x>\n",
        "ref `op:" + "//<vault>/<item>/<field>`.\n", "ref \"op:" + "//<vault>/<item>/<url-field>\",\n", "ref op:" + "//<vault>/<item>/<section>/<field>\n", "SU" + "A" * 57 + "\n"]
with tempfile.TemporaryDirectory() as tmp:
    tree(tmp, {**CLEAN, "docs/safe.md": "".join(SAFE)})
    code, out = run(tmp)
    check("other addresses, placeholders, public keys and prose are left alone", code == 0, out)

print("the op-reference and private-ip rules, attacked directly")
OP = "op:" + "//"
OP_CASES = {
    OP + "<vault>/<item>/<field>": False, OP + "<vault>/<item>/url": False, OP + "<vault>/<desk-a>/key": False, OP + "<x>": False, OP + "<vault>/": False,
    "`" + OP + "<vault>/<item>/<field>`.": False, '"' + OP + '<vault>/<item>/<url-field>",': False, OP + "<vault>/<item>/<section>/<field>": False,
    OP + "<vault>/<item>/url?attribute=otp": False, 'startswith("' + OP + '")': False, "see " + OP: False, OP + "...": False, OP + "..": False,
    'REF = "' + OP + '<vault>/<item>/<field>"\\nok = 1': False,
    OP + "<vault>/ActualItem/password": True, OP + "<vault>/<item>Secret/field": True, OP + "<vault>/<item>/PrivateField": True,
    OP + "<vault>/<item>/<field>x": True, OP + "<vault>/<item>/Section/<field>": True, OP + "Vault/Item/field": True, OP + "Vault/<item>/field": True,
    OP + ".hidden/x": True, OP + "/foo": True, OP + "<va ult>/x": True, OP + "<vault>/<item>/<field>/RealTail": True, OP + "<Real>Item/x": True,
}
wrong = [(text, want) for text, want in OP_CASES.items() if bool(scrub_check.OP_REFERENCE.search(text)) != want]
check("op-reference: every partly anonymised reference fails and every fully placeholder one passes", wrong == [], str(wrong))
IP_CASES = {   # the first dot is written as | and put back below, so this file holds no address for the gate to find
    "10|1.2.3": True, "x 10|1.2.3.": True, "10|1.2.3,": True, "(10|1.2.3)": True, "10|1.2.34": True, "v10|1.2.3": True,
    "10|1.2.3.4": False, "10|1.2.3456": False, "a.10|1.2.3": False, "1.10|1.2.3": False, "110|1.2.3": False, "172|16.0.1.5": False, "100|64.1.1.9": False,
    "192|168.0.1.2": False, "169|254.1.1.1": False,
}
wrong = [(text, want) for text, want in IP_CASES.items() if bool(scrub_check.PRIVATE_IP.search(text.replace("|", "."))) != want]
check("private-ip: a longer dotted token is not an address at either end, a sentence-ending dot or comma still is", wrong == [], str(wrong))

print("working-note and junk rules")
with tempfile.TemporaryDirectory() as tmp:
    tree(tmp, {**CLEAN, "HANDOFF.md": "notes\n"})
    code, out = run(tmp)
    check("HANDOFF.md at the root fails (the publish step must delete it first)", code == 1 and "HANDOFF.md: [handoff]" in out, out)
    code, out = run(tmp, "--skip-handoff-check")
    check("--skip-handoff-check lets a release in preparation pass", code == 0 and "0 hits" in out, out)
with tempfile.TemporaryDirectory() as tmp:
    tree(tmp, {**CLEAN, "sub/HANDOFF.md": "not a root handoff\n"})
    code, out = run(tmp)
    check("only a HANDOFF.md at the root counts", code == 0, out)
with tempfile.TemporaryDirectory() as tmp:
    tree(tmp, {**CLEAN, ".DS_Store": b"\x00\x01", "postbox/__pycache__/x.cpython-312.pyc": b"\x00\x01"})
    code, out = run(tmp)
    check(".DS_Store, __pycache__ and .pyc files fail as junk", code == 1 and out.count("[junk]") >= 3, out)
with tempfile.TemporaryDirectory() as tmp:
    tree(tmp, {**CLEAN, ".claude/scheduled_tasks.lock": "{}"})
    code, out = run(tmp)
    check("a .lock file fails as junk", code == 1 and "[junk]" in out, out)
for junk_name in ("postbox/._lib.py", "Thumbs.db", "tests/x.swp", "README.md~", "a.orig", ".localized"):
    with tempfile.TemporaryDirectory() as tmp:
        tree(tmp, {**CLEAN, junk_name: "x"})
        code, out = run(tmp)
        check(f"{junk_name} fails as junk", code == 1 and "[junk]" in out, out)
with tempfile.TemporaryDirectory() as tmp:
    tree(tmp, {**CLEAN, ".venv/lib/x.py": f"cd {HOME}\n", ".git/config": f"url = {HOME}\n", "img.png": b"\x89PNG\x00" + HOME.encode()})
    code, out = run(tmp)
    check(".venv, .git and binary files are not scanned", code == 0, out)

print("denylist")
with tempfile.TemporaryDirectory() as tmp:
    deny = Path(tmp, "outside-deny.txt")
    deny.write_text("# private strings\n\nAcmeCorp\nproject-zeta\n@allow LICENSE\n")
    repo = Path(tmp, "repo")
    tree(repo, {**CLEAN, "docs/a.md": "written by ACMECORP staff\n", "LICENSE": "Copyright 2026 AcmeCorp\n", "notes-project-zeta.txt": "x\n"})
    code, out = run(repo, "--denylist", str(deny))
    check("a denylist string fails, whatever its case", code == 1 and "docs/a.md:1: [denylist]" in out, out)
    check("a denylist string in a FILE NAME fails too", "notes-project-zeta.txt: [denylist]" in out, out)
    check("a file named by @allow is exempt from the denylist", "LICENSE" not in out, out)
    check("two hits in this tree, and nothing else", out.strip().endswith("scrub gate: 2 hits"), out)
    code, out = run(repo)
    check("without --denylist the same tree has no hits (the tool ships with no list of names)", code == 0 and "0 hits" in out, out)
    code, out = run(repo, "--denylist", str(Path(tmp, "missing.txt")))
    check("a denylist that does not exist is an error (exit 2), never a silent pass", code == 2 and "not found" in out, out)
    code, out = run(Path(tmp, "no-such-folder"))
    check("a root that is not a folder is an error (exit 2)", code == 2, out)
    for label, body in (("empty", b""), ("only comments and blank lines", b"# nothing here\n\n"), ("only @allow lines", b"@allow LICENSE\n"), ("not UTF-8 text", b"\xff\xfe\x00bad\n")):
        bad = Path(tmp, "bad-deny.txt")
        bad.write_bytes(body)
        code, out = run(repo, "--denylist", str(bad))
        check(f"a denylist that is {label} is an error (exit 2), never a silent 0 hits", code == 2 and "0 hits" not in out and "Traceback" not in out, out)
with tempfile.TemporaryDirectory() as tmp:
    deny = Path(tmp, "d.txt")
    deny.write_text("same-secret\n")
    repo = Path(tmp, "repo")
    tree(repo, {**CLEAN, "a.md": "same-secret and same-secret\nsame-secret\n"})
    code, out = run(repo, "--denylist", str(deny))
    check("every line with a hit is reported with its line number", code == 1 and "a.md:1:" in out and "a.md:2:" in out, out)

print("this repo")
hits = [h for h in scrub_check.scan(ROOT, None, skip_handoff_check=True) if h[2] != "junk"]
check("the generic checks find nothing in this repo (bytecode folders from other test runs do not count here)", hits == [], str(hits[:5]))
import re  # noqa: E402
late = []
for suite in sorted(Path(ROOT, "tests").glob("test_*.py")):
    if suite.name == "test_env_guard.py":
        continue   # imports no helper itself: it runs every case in a child started with -B (its "import _env" lines are inside strings)
    src = suite.read_text().splitlines()
    first_helper = next((i for i, line in enumerate(src) if re.match(r"import (_env|_guard)\b", line)), None)
    if first_helper is not None and not any("sys.dont_write_bytecode = True" in line for line in src[:first_helper]):
        late.append(suite.name)
check("every suite turns bytecode off BEFORE its first tests/ helper import (else a test run leaves tests/__pycache__)", late == [], str(late))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
