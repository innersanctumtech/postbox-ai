#!/usr/bin/env python3
"""Probes for tests/_guard.py (imported by tests/_env.py); what the guard covers and does not cover is in the docstring at the top of that file.

The guard probes run in guarded child processes against a stand-in listener on a spare port. The parent process opens that listener, looks up this Mac's local addresses
(a UDP connect to a documentation-range address, which sends nothing, and /sbin/ifconfig) and checks the suite inventory and the doc text.
If the first child cannot establish the operating-system layer, this suite prints FAILED (not skipped) and exits 1, not 3 (the child is what exits 3).
19 of the 21 suites import the guard directly; the other two are this one and tests/test_scrub_check.py, a static scanner.
Run: python3 tests/test_env_guard.py   (from a normal Terminal on macOS, not from inside another sandbox)
"""
import os
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def child(code, **env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("POSTBOX_")}
    base.update(env)
    try:
        r = subprocess.run([sys.executable, "-B", "-c", f"import sys; sys.path.insert(0, {str(TESTS)!r})\n{code}"], env=base, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return -9, "", "timed out after 60 s"   # a failed case, not a crashed test run
    return r.returncode, r.stdout.strip(), r.stderr.strip()


print("the operating-system layer is required, and it is decided by trying (never by a marker)")
code, out, err = child("import _env, _guard; print(_guard.kernel_enforced())")
check("a guarded child is under the kernel profile (kernel_enforced is True)", code == 0 and out == "True", f"exit {code} {out!r} {err}")
if code != 0 or out != "True":
    print("\nThe operating-system layer could not be established, so no further case can be trusted. FAILED (not skipped).")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1)

print("refusing the default address")
for url in ("nats://127.0.0.1:4222", "nats://127.0.0.1", "nats://localhost:4222", "127.0.0.1:4222", "nats://user:pw@127.0.0.1:4222", "nats://[::1]:4222"):
    code, out, err = child("import _env", POSTBOX_NATS_URL=url)
    check(f"POSTBOX_NATS_URL={url} makes _env exit with code 3 and say why", code == 3 and "tests refuse to run" in err and "default post box address" in err, f"{code} {err}")
code, out, err = child("import _guard", POSTBOX_NATS_URL="nats://127.0.0.1:4222")
check("so does _guard on its own (suites that do not use _env)", code == 3, f"{code} {err}")
code, out, err = child("import _env; print(1)", POSTBOX_NATS_URL="nats://127.0.0.1:54321")
check("another port is fine", code == 0 and out == "1", err)
code, out, err = child("import _env; print(1)", POSTBOX_NATS_URL="nats://other-host.example:4222")
check("port 4222 on ANOTHER host is not this Mac's post box, so it is not refused", code == 0 and out == "1", err)

print("when nothing is set")
code, out, err = child("import _env, lib, _guard; print(lib.NATS_URL, _guard.url_port(lib.NATS_URL)[0])")
parts = out.split()
check("lib.NATS_URL points at a spare port, never 4222", code == 0 and parts[0].startswith("nats://127.0.0.1:") and parts[1].isdigit() and int(parts[1]) != 4222, f"{out} {err}")
code, out, err = child("import _env, os; print(os.environ['POSTBOX_SUPPORT_DIR'])")
check("the support folder is a throwaway one", code == 0 and "postbox-test-support-" in out, f"{out} {err}")
code, out, err = child("import _env, os; print(os.environ['POSTBOX_SUPPORT_DIR'])", POSTBOX_SUPPORT_DIR="/tmp/mine")
check("a support folder a test chose is kept", out == "/tmp/mine", f"{out} {err}")

print("the connection guard works even when a test wipes the environment")
code, out, err = child("""
import _env, os, socket
for k in list(os.environ):
    if k.startswith('POSTBOX_'): del os.environ[k]
s = socket.socket()
try:
    s.connect(('127.0.0.1', 4222))
except BaseException as e:
    print(type(e).__name__)
""")
check("a connect to 127.0.0.1:4222 raises BrokerLeak", out == "BrokerLeak", f"{out} {err}")
code, out, err = child("""
import _env, socket
try:
    socket.socket().connect_ex(('localhost', 4222))
except BaseException as e:
    print(type(e).__name__)
""")
check("connect_ex and the name localhost are covered too", out == "BrokerLeak", f"{out} {err}")
code, out, err = child("""
import _env, socket
try:
    try:
        socket.socket().connect(('127.0.0.1', 4222))
    except Exception:
        print('swallowed')
except BaseException as e:
    print(type(e).__name__)
""")
check("a broad `except Exception` cannot swallow it (BrokerLeak is a BaseException)", out == "BrokerLeak", f"{out} {err}")
code, out, err = child("""
import _env, asyncio
async def go():
    try:
        await asyncio.open_connection('127.0.0.1', 4222)
    except BaseException as e:
        print(type(e).__name__)
asyncio.run(go())
""")
check("an asyncio connection (what the NATS client uses) is stopped as well", out == "BrokerLeak", f"{out} {err}")
code, out, err = child("""
import _env, lib, post, asyncio
try:
    asyncio.run(lib.connect_fast(lib.local_target(), timeout=2))
except lib.MailboxError as e:
    print('MailboxError', lib.NATS_URL, str(e))
""")
dead = re.match(r"^MailboxError nats://127\.0\.0\.1:(\d+) ", out)
check("lib.connect_fast with no override tries the guard's dead spare port (never 4222) and fails with the connection-refused MailboxError",
      code == 0 and dead is not None and dead.group(1) != "4222" and f"ConnectionRefusedError(61, \"Connect call failed ('127.0.0.1', {dead.group(1)})\")" in out, f"{code} {out} {err}")
code, out, err = child("""
import _env, lib, asyncio
async def go():
    raise ZeroDivisionError('an unrelated failure')
try:
    asyncio.run(go())
except lib.MailboxError:
    print('MailboxError')
""")
check("an unrelated exception in such a case is NOT a pass (the case above prints only for the connection failure)", code != 0 and "MailboxError" not in out and "ZeroDivisionError" in err, f"{code} {out} {err}")

print("the guard follows every child process, whatever environment the caller gives it")
stand_in = socket.socket()
stand_in.bind(("127.0.0.1", 0))
stand_in.listen(8)
stand_in.settimeout(0.2)
STAND_PORT = stand_in.getsockname()[1]
heard = []


def accept_loop():
    while stand_in.fileno() != -1:
        try:
            heard.append(stand_in.accept())
        except (socket.timeout, OSError):
            continue


threading.Thread(target=accept_loop, daemon=True).start()
TRY = f"import socket\ns = socket.socket()\ntry:\n    s.connect(('127.0.0.1', {STAND_PORT}))\n    print('connected')\nexcept BaseException as e:\n    print(type(e).__name__)\n"
code, out, err = child(TRY)
time.sleep(0.4)
check("positive control: a process WITHOUT the guard reaches the stand-in listener (so the cases below could fail)", out == "connected" and len(heard) == 1, f"{out} {err} {len(heard)}")
del heard[:]
GUARDED = {"PBGUARD_PORTS": str(STAND_PORT)}   # the Python patches raise BrokerLeak before the kernel is asked, so an expected "BrokerLeak" proves the Python layer
variants = {
    "env wiped of POSTBOX_*": "{k: v for k, v in os.environ.items() if not k.startswith('POSTBOX_')}",
    "an empty env": "{}",
    "no env argument": "None",
    "an env that names a different broker port": "{'POSTBOX_NATS_URL': 'nats://127.0.0.1:1'}",
}
for label, env_expr in variants.items():
    code, out, err = child(f"""
import _env, os, subprocess, sys
r = subprocess.run([sys.executable, '-B', '-c', {TRY!r}], env={env_expr}, capture_output=True, text=True, timeout=60)
print(r.stdout.strip() or r.stderr.strip()[-200:])
""", **GUARDED)
    check(f"a Python child started with {label} cannot connect to a protected port (BrokerLeak)", out == "BrokerLeak", f"{code} {out} {err}")
NEST = ("import subprocess, sys\nr = subprocess.run([sys.executable, '-B', '-c', " + repr(TRY) + "], env={}, capture_output=True, text=True, timeout=60)\n"
        "print(r.stdout.strip() or r.stderr.strip()[-200:])\n")
code, out, err = child(f"""
import _env, subprocess, sys
r = subprocess.run([sys.executable, '-B', '-c', {NEST!r}], env={{}}, capture_output=True, text=True, timeout=60)
print(r.stdout.strip() or r.stderr.strip()[-200:])
""", **GUARDED)
check("and so does a grandchild started with an empty env by a child that was itself started with an empty env", out == "BrokerLeak", f"{code} {out} {err}")
code, out, err = child(f"""
import _env, os
os.execve(__import__('sys').executable, [__import__('sys').executable, '-B', '-c', {TRY!r}], {{}})
""", **GUARDED)
check("os.execve with a hand-built empty env is covered too", out == "BrokerLeak", f"{code} {out} {err}")
code, out, err = child(f"""
import _env, os, sys
os.posix_spawn(sys.executable, [sys.executable, '-B', '-c', {TRY!r}], {{}})
import time; time.sleep(2)
""", **GUARDED)
check("and os.posix_spawn", out == "BrokerLeak", f"{code} {out} {err}")
LIB = f"import sys; sys.path.insert(0, {str(TESTS.parent / 'postbox')!r}); import lib, socket; print(lib.NATS_URL, socket.socket.connect._pbguard)"
code, out, err = child(f"""
import _env, os, subprocess, sys
r = subprocess.run([sys.executable, '-B', '-c', {LIB!r}], env={{k: v for k, v in os.environ.items() if not k.startswith('POSTBOX_')}},
                   capture_output=True, text=True, timeout=60)
print(r.stdout.strip() or r.stderr.strip()[-300:])
""", **GUARDED)
check("a wiped-env child that imports the production module sees the production default URL, with the connection guard already on", out == "nats://127.0.0.1:4222 True", f"{code} {out} {err}")
for label, argv in (("-I", "'-I'"), ("-S", "'-S'"), ("-E", "'-E'"), ("-BI", "'-BI'"), ("-X dev -I", "'-X', 'dev', '-I'")):
    code, out, err = child(f"""
import _env, subprocess, sys
try:
    subprocess.run([sys.executable, {argv}, '-c', 'print(1)'], capture_output=True, timeout=60)
    print('started')
except BaseException as e:
    print(type(e).__name__)
""", **GUARDED)
    check(f"starting Python with {label} (it would skip the guard) is refused", out == "BrokerLeak", f"{code} {out} {err}")
code, out, err = child("""
import _env, subprocess, sys
try:
    subprocess.run('python3 -I -c "print(1)"', shell=True, capture_output=True, timeout=60)
    print('started')
except BaseException as e:
    print(type(e).__name__)
""", **GUARDED)
check("and so is the same thing through a shell string", out == "BrokerLeak", f"{code} {out} {err}")
code, out, err = child("""
import _env, subprocess, sys
r = subprocess.run([sys.executable, '-B', '-c', 'print(1)'], capture_output=True, text=True, timeout=60)
print(r.stdout.strip())
""", **GUARDED)
check("a normal child (and -B, -c) still starts", out == "1", f"{code} {out} {err}")
code, out, err = child("""
import _env, subprocess, sys
try:
    subprocess.run([sys.executable, '-B', '-c', 'print(1)'], env={'POSTBOX_NATS_URL': 'nats://127.0.0.1:4222'}, capture_output=True, timeout=60)
    print('started')
except BaseException as e:
    print(type(e).__name__)
""", **GUARDED)
check("a child whose environment names the default post box address is refused before it starts", out == "BrokerLeak", f"{code} {out} {err}")
code, out, err = child(f"""
import _env, socket
for host in ('127.1', '0x7f.1', '2130706433', 'LOCALHOST.', '0.0.0.0', '::ffff:127.0.0.1', 'localhost', 'x.localhost'):
    try:
        socket.socket().connect((host, {STAND_PORT}))
        print(host, 'connected')
    except BaseException as e:
        print(host, type(e).__name__)
""", **GUARDED)
check("every spelling of this Mac (127.1, hex, integer, trailing dot, 0.0.0.0, ::ffff:127.0.0.1, *.localhost) is covered",
      out.splitlines() == [f"{h} BrokerLeak" for h in ("127.1", "0x7f.1", "2130706433", "LOCALHOST.", "0.0.0.0", "::ffff:127.0.0.1", "localhost", "x.localhost")], f"{code} {out} {err}")
time.sleep(0.4)
check("the stand-in listener heard nothing from any guarded process in this section", heard == [], str(heard))

print("which Python command lines skip the guard (the parser, both directions)")
SKIP_TABLE = [
    (["-I", "-c", "1"], True), (["-S", "x.py"], True), (["-E", "-c", "1"], True), (["-BI", "-c", "1"], True), (["-OO", "-S"], True), (["-Ic", "1"], True),
    (["--check-hash-based-pycs", "always", "-I", "-c", "1"], True), (["--check-hash-based-pycs=always", "-S"], True),
    (["-W", "error", "-E", "x.py"], True), (["-X", "dev", "-I"], True), (["-Wdefault", "-S"], True), (["-IW", "error"], True),
    (["-c", "-I"], False), (["-cI"], False), (["-m", "pip", "-I"], False), (["x.py", "-I"], False), (["-"], False), (["--", "-I"], False),
    (["-B", "-O", "-c", "1"], False), (["-W", "ignore::Error", "-c", "1"], False), (["-Wdefault", "-c", "1"], False), (["-X", "importtime", "-c", "1"], False),
    (["--check-hash-based-pycs", "always", "-c", "1"], False), (["--version"], False), ([], False),
]
code, out, err = child(f"import _guard, json\nprint(json.dumps([[a, _guard._python_skips_guard(a), want] for a, want in {SKIP_TABLE!r}]))")
rows = json.loads(out) if code == 0 and out else []
wrong = [r for r in rows if r[1] != r[2]]
check(f"all {len(SKIP_TABLE)} command lines are classified correctly", len(rows) == len(SKIP_TABLE) and not wrong, f"{code} {wrong} {err}")

print("the launch boundary: a child is either followed by the guard or refused (the Python layer answers first, with BrokerLeak, before the kernel is asked)")
WORK = Path(tempfile.mkdtemp(prefix="pbguard-test-"))
PY = sys.executable
TRY_CODE = f"import socket\ns = socket.socket()\ntry:\n    s.connect(('127.0.0.1', {STAND_PORT}))\n    print('connected')\nexcept BaseException as e:\n    print(type(e).__name__)\n"
TRY_FILE = WORK / "try.py"
TRY_FILE.write_text(TRY_CODE)


def script(name, text):
    path = WORK / name
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


S_UNSET = script("unset.sh", f"#!/bin/sh\nunset PYTHONPATH PBGUARD_ACTIVE\nexec {PY} {TRY_FILE}\n")
S_NC = script("nc.sh", f"#!/bin/sh\n/usr/bin/nc -z 127.0.0.1 {STAND_PORT}\n")
S_CLEAN = script("clean.sh", f"#!/bin/sh\nexec {PY} {TRY_FILE}\n")
S_SHEBANG_I = script("shebang_i.py", f"#!{PY} -I\n{TRY_CODE}")
S_SHEBANG = script("shebang.py", f"#!{PY}\n{TRY_CODE}")
RUN = "capture_output=True, text=True, timeout=60"
P, T = repr(PY), repr(TRY_CODE)


def launch(body):
    return child("import _env, os, subprocess, sys, multiprocessing.util\n" + body, **GUARDED)


def refused(label, expr):
    code, out, err = launch(f"try:\n    {expr}\n    print('started')\nexcept BaseException as e:\n    print(type(e).__name__)\n")
    check(f"refused before it starts: {label}", out == "BrokerLeak", f"{code} {out} {err}")


def followed(label, expr):
    code, out, err = launch(f"{expr}\n")
    check(f"started, and the guard is on in it (BrokerLeak): {label}", out == "BrokerLeak", f"{code} {out} {err}")


REFUSED = {
    "python --check-hash-based-pycs always -I": f"subprocess.run([{P}, '--check-hash-based-pycs', 'always', '-I', '-c', 'print(1)'], {RUN})",
    "python --check-hash-based-pycs=always -S": f"subprocess.run([{P}, '--check-hash-based-pycs=always', '-S', '-c', 'print(1)'], {RUN})",
    "python -W error -E": f"subprocess.run([{P}, '-W', 'error', '-E', '-c', 'print(1)'], {RUN})",
    "python -X dev -S": f"subprocess.run([{P}, '-X', 'dev', '-S', '-c', 'print(1)'], {RUN})",
    "a program called custom-name that executable= turns into Python -I": f"subprocess.run(['custom-name', '-I', '-c', 'print(1)'], executable={P}, {RUN})",
    "env -i python": f"subprocess.run(['/usr/bin/env', '-i', {P}, '-c', {T}], {RUN})",
    "env -u PYTHONPATH python": f"subprocess.run(['/usr/bin/env', '-u', 'PYTHONPATH', {P}, '-c', {T}], {RUN})",
    "env PYTHONPATH=/nowhere python": f"subprocess.run(['/usr/bin/env', 'PYTHONPATH=/nowhere', {P}, '-c', {T}], {RUN})",
    "env PBGUARD_ACTIVE=0 python": f"subprocess.run(['/usr/bin/env', 'PBGUARD_ACTIVE=0', {P}, '-c', {T}], {RUN})",
    "env -S": f"subprocess.run(['/usr/bin/env', '-S', {P + ' -c 1'!r}], {RUN})",
    "env wrapped around python -I": f"subprocess.run(['/usr/bin/env', 'FOO=1', {P}, '-I', '-c', 'print(1)'], {RUN})",
    "/bin/sh -c 'unset PYTHONPATH PBGUARD_ACTIVE; exec python3 ...'": f"subprocess.run(['/bin/sh', '-c', 'unset PYTHONPATH PBGUARD_ACTIVE; exec python3 -c \"print(1)\"'], {RUN})",
    "bash -c with an operator": f"subprocess.run(['/bin/bash', '-c', 'true && true'], {RUN})",
    "a shell that would read its commands from stdin": f"subprocess.run(['/bin/sh'], {RUN})",
    "sh -c 'python -I ...'": f"subprocess.run(['/bin/sh', '-c', {P + ' -I -c 1'!r}], {RUN})",
    "shell=True with python -S": f"subprocess.run({P + ' -S -c 1'!r}, shell=True, {RUN})",
    "shell=True with an operator": f"subprocess.run('true; true', shell=True, {RUN})",
    "shell=True with a $ expansion": f"subprocess.run('echo $HOME', shell=True, {RUN})",
    "a shell script that unsets the guard variables": f"subprocess.run(['/bin/sh', {S_UNSET!r}], {RUN})",
    "the same script run directly": f"subprocess.run([{S_UNSET!r}], {RUN})",
    "a shell script that calls nc": f"subprocess.run([{S_NC!r}], {RUN})",
    "a script whose #! line is python -I": f"subprocess.run([{S_SHEBANG_I!r}], {RUN})",
    "os.system with an operator": "os.system('true && true')",
    "os.system with python -I": f"os.system({P + ' -I -c 1'!r})",
    "os.execv python -I": f"os.execv({P}, [{P}, '-I', '-c', '1'])",
    "os.execvp python -S": f"os.execvp({P}, [{P}, '-S', '-c', '1'])",
    "os.execve python -E with {}": f"os.execve({P}, [{P}, '-E', '-c', '1'], {{}})",
    "os.spawnv python -I": f"os.spawnv(os.P_WAIT, {P}, [{P}, '-I', '-c', '1'])",
    "os.spawnvp python -S": f"os.spawnvp(os.P_WAIT, {P}, [{P}, '-S', '-c', '1'])",
    "os.spawnve python -E with {}": f"os.spawnve(os.P_WAIT, {P}, [{P}, '-E', '-c', '1'], {{}})",
    "os.posix_spawn python -I with {}": f"os.posix_spawn({P}, [{P}, '-I', '-c', '1'], {{}})",
    "multiprocessing.util.spawnv_passfds python -I": f"multiprocessing.util.spawnv_passfds({P}.encode(), [{P}.encode(), b'-I', b'-c', b'1'], ())",
    f"nc to the stand-in port": f"subprocess.run(['/usr/bin/nc', '-z', '127.0.0.1', '{STAND_PORT}'], {RUN})",
    "curl": f"subprocess.run(['/usr/bin/curl', '-s', 'http://127.0.0.1:{STAND_PORT}/'], {RUN})",
    "perl": f"subprocess.run(['/usr/bin/perl', '-e', '1'], {RUN})",
    "osascript": f"subprocess.run(['/usr/bin/osascript', '-e', '1'], {RUN})",
}
for label, expr in REFUSED.items():
    refused(label, expr)

FOLLOWED = {
    "python -W error": f"r = subprocess.run([{P}, '-W', 'error', '-c', {T}], {RUN}); print(r.stdout.strip())",
    "python --check-hash-based-pycs always": f"r = subprocess.run([{P}, '--check-hash-based-pycs', 'always', '-c', {T}], {RUN}); print(r.stdout.strip())",
    "executable= with another argv[0]": f"r = subprocess.run(['custom-name', '-c', {T}], executable={P}, {RUN}); print(r.stdout.strip())",
    "env NAME=value python": f"r = subprocess.run(['/usr/bin/env', 'FOO=1', {P}, '-c', {T}], {RUN}); print(r.stdout.strip())",
    "sh -c 'python script'": f"r = subprocess.run(['/bin/sh', '-c', {P + ' ' + str(TRY_FILE)!r}], {RUN}); print(r.stdout.strip())",
    "shell=True 'python script'": f"r = subprocess.run({P + ' ' + str(TRY_FILE)!r}, shell=True, {RUN}); print(r.stdout.strip())",
    "a clean shell script": f"r = subprocess.run([{S_CLEAN!r}], {RUN}); print(r.stdout.strip())",
    "a script with a plain python #! line": f"r = subprocess.run([{S_SHEBANG!r}], {RUN}); print(r.stdout.strip())",
    "os.system": f"os.system({P + ' ' + str(TRY_FILE)!r})",
    "os.execv": f"os.execv({P}, [{P}, {str(TRY_FILE)!r}])",
    "os.execve with {}": f"os.execve({P}, [{P}, {str(TRY_FILE)!r}], {{}})",
    "os.spawnv": f"os.spawnv(os.P_WAIT, {P}, [{P}, {str(TRY_FILE)!r}])",
    "os.spawnve with {}": f"os.spawnve(os.P_WAIT, {P}, [{P}, {str(TRY_FILE)!r}], {{}})",
    "os.posix_spawn with {}": f"os.waitpid(os.posix_spawn({P}, [{P}, {str(TRY_FILE)!r}], {{}}), 0)",
    "multiprocessing.util.spawnv_passfds (no env at all)": f"import time; os.waitpid(multiprocessing.util.spawnv_passfds({P}.encode(), [{P}.encode(), {str(TRY_FILE)!r}.encode()], ()), 0)",
}
for label, expr in FOLLOWED.items():
    followed(label, expr)
code, out, err = launch(f"""
rows = [subprocess.run(cmd, capture_output=True, text=True, timeout=60).returncode for cmd in (['git', '--version'], ['ps', '-p', str(os.getpid()), '-o', 'etime='], ['lsof', '-a', '-p', str(os.getpid()), '-d', 'cwd', '-Fn'], ['plutil', '-help'])]
print(rows)
""")
check("the tools the suites need (git, ps, lsof, plutil) still start", code == 0 and out.startswith("[0, 0, 0") , f"{code} {out} {err}")
code, out, err = launch("""
try:
    subprocess.run(['no-such-program-anywhere'], capture_output=True)
except BaseException as e:
    print(type(e).__name__)
""")
check("a program that does not exist is an ordinary FileNotFoundError, not a guard refusal", out == "FileNotFoundError", f"{code} {out} {err}")
time.sleep(0.4)
check("the stand-in listener heard nothing from any refused or followed launch above", heard == [], str(heard))

print("the operating-system layer: a protected port is refused for processes the Python patches never see")
SOCK_PROBE = WORK / "probe.py"
SOCK_PROBE.write_text(f"import socket\ns = socket.socket()\ntry:\n    s.connect(('127.0.0.1', {STAND_PORT}))\n    print('connected')\nexcept OSError as e:\n    print(type(e).__name__, e.errno)\n")
OUT = WORK / "native.out"
NATIVE = {   # each line is run by the C library's system(): no Python launch API is involved, so only the operating system can stop it
    "python -I -S started by env -i (no variable, no site, no guard)": f"/usr/bin/env -i {PY} -I -S {SOCK_PROBE} > {OUT}",
    "python after a shell unset the guard's variables": f"/bin/sh -c 'unset PYTHONPATH PBGUARD_ACTIVE; exec {PY} {SOCK_PROBE}' > {OUT}",
}
BASE_PY = Path(getattr(sys, "_base_executable", "") or sys.executable)   # the interpreter the virtual environment was made from (sys.base_prefix/bin/python3 does not exist for a Homebrew framework build)
check("the base interpreter (a case below) exists, so that case cannot be dropped silently", BASE_PY.exists(), str(BASE_PY))
if BASE_PY.exists():
    NATIVE["the base interpreter, not the virtual environment's, with -I"] = f"{BASE_PY} -I {SOCK_PROBE} > {OUT}"
for label, cmdline in NATIVE.items():
    code, out, err = child(f"import _env, ctypes\nctypes.CDLL(None).system({cmdline!r}.encode())\nprint(open({str(OUT)!r}).read().strip())", PBGUARD_PORTS=str(STAND_PORT))
    check(f"refused by the operating system (PermissionError, errno 1): {label}", out == "PermissionError 1", f"{code} {out} {err}")
NC_NATIVE = f"import _env, _guard, ctypes\nrc = ctypes.CDLL(None).system(b'/usr/bin/nc -z 127.0.0.1 {STAND_PORT}')\nprint(rc != 0)"
code, out, err = child(NC_NATIVE, PBGUARD_PORTS=str(STAND_PORT))
check("a compiled client (nc) started through libc is refused by the operating system as well", out == "True", f"{code} {out} {err}")
code, out, err = child(f"import _env, ctypes\nrc = ctypes.CDLL(None).system(b'/bin/ps -p 1 -o pid= > {OUT}')\nprint(rc, open({str(OUT)!r}).read().strip())", PBGUARD_PORTS=str(STAND_PORT))
check("the one exception (/bin/ps, a setuid tool the operating system will not start in a sandbox) still works", out == "0 1", f"{code} {out} {err}")
time.sleep(0.4)
check("the stand-in listener heard nothing from the sandboxed cases", heard == [], str(heard))
for label, cmdline in (("python -I -S via env -i", NATIVE["python -I -S started by env -i (no variable, no site, no guard)"]), ("nc", f"/usr/bin/nc -z 127.0.0.1 {STAND_PORT}")):
    code, out, err = child(f"import ctypes\nrc = ctypes.CDLL(None).system({cmdline!r}.encode())\nprint(rc)")
    time.sleep(0.2)
    check(f"positive control: the same {label} line WITHOUT the guard does reach the stand-in listener", out == "0" and len(heard) >= 1, f"{code} {out} {err} {len(heard)}")
    del heard[:]

print("the operating-system layer is decided by trying: a marker, in any state, neither skips it nor stands in for it")
CANARY = "import socket\ns = socket.socket()\nprint(s.connect_ex(('127.0.0.1', 1)))"
code, out, err = child(CANARY)
check("positive control: without the profile, the canary port answers ECONNREFUSED (61), not EPERM (1), so kernel_enforced can tell the two apart", out == "61", f"{code} {out} {err}")
for forged in ("1", "0", "off", "verified", ""):
    code, out, err = child(NC_NATIVE + "\nprint(_guard.kernel_enforced())", PBGUARD_PORTS=str(STAND_PORT), PBGUARD_KERNEL=forged)
    check(f"PBGUARD_KERNEL={forged!r} set by hand on an unsandboxed process changes nothing: the layer is applied anyway (nc refused, kernel_enforced True)",
          code == 0 and out.splitlines()[-2:] == ["True", "True"], f"{code} {out} {err}")
FOREIGN = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", PY, "-B", "-c", f"import sys; sys.path.insert(0, {str(TESTS)!r})\nimport _env\nprint('started')"]
for forged in (None, "1", "0", "off"):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTBOX_", "PBGUARD_"))}
    if forged is not None:
        env["PBGUARD_KERNEL"] = forged
    r = subprocess.run(FOREIGN, env=env, capture_output=True, text=True, timeout=60)
    check(f"inside another sandbox (no second one can be applied) the guard refuses to run, exit 3, and says why{'' if forged is None else f' (PBGUARD_KERNEL={forged!r} does not help)'}",
          r.returncode == 3 and "started" not in r.stdout and "operating-system layer" in r.stderr and "sandbox" in r.stderr, f"{r.returncode} {r.stdout!r} {r.stderr[-300:]!r}")
code, out, err = child("import _env; print('started')", PBGUARD_REEXEC="1")
check("PBGUARD_REEXEC set by hand on a process that is not under the profile ends in the same refusal (exit 3), never in a loop or a pass",
      code == 3 and "started" not in out and "operating-system layer" in err, f"{code} {out} {err}")
code, out, err = child("import _guard; print(sorted(_guard.PROTECTED)); print(_guard.kernel_profile({4222}))")
check("the kernel profile names port 4222 and the canary port, and the protected set always has 4222", code == 0 and "localhost:4222" in out and "localhost:1\"" in out and "[4222]" in out.splitlines()[0], f"{code} {out} {err}")
code, out, err = child("import _env; print('started')", PBGUARD_PLIST_DIRS="/nonexistent-folder")
check("a folder of launchd job files that does not exist is not an error", code == 0 and out == "started" and err == "", f"{code} {out} {err}")

print("the profile names `localhost`: every address of this Mac, and the unspecified address, is refused by the kernel itself")
own = {"127.0.0.1", "0.0.0.0"}
for how in (lambda: socket.gethostbyname(socket.gethostname()), lambda: [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)][0]):
    try:
        own.add(how())
    except (OSError, IndexError):
        pass
try:
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.connect(("192.0.2.1", 9))        # no packet is sent: this only asks the routing table which local address a route would use
    own.add(udp.getsockname()[0])
    udp.close()
except OSError:
    pass
ifconfig = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True).stdout
own.update(m.group(1) for m in re.finditer(r"\binet (\d+\.\d+\.\d+\.\d+)\b", ifconfig))
OWN_V4 = sorted(own)


def kernel_only(guarded):
    """Connect to the stand-in port on every address of this Mac, with the unpatched socket method (the Python layer is bypassed on purpose)."""
    head = "import _env, _guard, " if guarded else "import "
    call = "_guard._REAL_CONNECT_EX(s, " if guarded else "s.connect_ex("
    return (f"{head}socket, json\nrows = {{}}\n"
            f"for fam, host in [(socket.AF_INET, h) for h in {OWN_V4!r}] + [(socket.AF_INET6, '::1')]:\n"
            f"    s = socket.socket(fam); s.settimeout(5)\n"
            f"    rows[host] = {call}(host, {STAND_PORT}))\n"
            "print(json.dumps(rows))")


code, out, err = child(kernel_only(True), PBGUARD_PORTS=str(STAND_PORT))
rows = json.loads(out) if code == 0 and out else {}
check(f"the kernel answers EPERM for all of {OWN_V4} and ::1 (the Python layer bypassed on purpose)", rows and all(v == 1 for v in rows.values()) and len(rows) == len(OWN_V4) + 1, f"{code} {rows} {err}")
code, out, err = child(kernel_only(False))
rows = json.loads(out) if code == 0 and out else {}
check(f"positive control: with no profile the same connections are not EPERM (answers seen: {sorted(set(rows.values()))}) and the stand-in does hear them",
      rows and all(v != 1 for v in rows.values()) and (time.sleep(0.4) or len(heard) >= 1), f"{code} {rows} {err} {len(heard)}")
del heard[:]
time.sleep(0.3)
check("the stand-in listener heard nothing from the kernel-only cases", heard == [], str(heard))

print("a post box installed on another port is found without being told (read-only from the launchd job files)")
FIXTURE = WORK / "agents"
FIXTURE.mkdir()
PORT_B = spare = None
for _ in range(2):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    if PORT_B is None:
        PORT_B = probe.getsockname()[1]
    else:
        spare = probe.getsockname()[1]
    probe.close()
import plistlib
(FIXTURE / "com.example.relay.plist").write_bytes(plistlib.dumps({"Label": "x", "EnvironmentVariables": {"POSTBOX_NATS_URL": f"nats://127.0.0.1:{STAND_PORT}"}}))
(FIXTURE / "mail.nats.plist").write_bytes(plistlib.dumps({"Label": "y", "ProgramArguments": ["/opt/homebrew/bin/nats-server", "-p", str(PORT_B), "-c", "/nowhere.conf"]}))
(FIXTURE / "remote.postbox.plist").write_bytes(plistlib.dumps({"Label": "z", "EnvironmentVariables": {"POSTBOX_NATS_URL": f"nats://other-host.example:{spare}"}}))
(FIXTURE / "unrelated.plist").write_bytes(plistlib.dumps({"Label": "u", "EnvironmentVariables": {"POSTBOX_NATS_URL": "nats://127.0.0.1:not-a-port"}, "ProgramArguments": ["/bin/echo"]}))
(FIXTURE / "broken.postbox.plist").write_bytes(b"this is not a property list")
DETECT = (f"import _env, _guard, socket, ctypes\nprint(sorted(_guard.PROTECTED))\ns = socket.socket()\n"
          f"try:\n    s.connect(('127.0.0.1', {STAND_PORT}))\n    print('connected')\nexcept BaseException as e:\n    print(type(e).__name__)\n"
          f"print(ctypes.CDLL(None).system(b'/usr/bin/nc -z 127.0.0.1 {STAND_PORT}') != 0)")
code, out, err = child(DETECT, PBGUARD_PLIST_DIRS=str(FIXTURE))     # no PBGUARD_PORTS: the ports come from the job files alone
lines = out.splitlines()
check("the ports in the job files (a relay's POSTBOX_NATS_URL, a nats-server -p) are protected with 4222; a remote host's port and a malformed URL are not",
      code == 0 and lines[:1] == [str(sorted({4222, STAND_PORT, PORT_B}))], f"{code} {out} {err}")
check("a connection to the installed stand-in port raises BrokerLeak, and a native client is refused by the kernel, with no PBGUARD_PORTS given",
      lines[1:] == ["BrokerLeak", "True"], f"{code} {out} {err}")
check("a job file that mentions a post box but cannot be parsed is NAMED in a warning (never a silent gap), and the run goes on",
      "broken.postbox.plist" in err and "cannot be parsed" in err and "unrelated.plist" not in err, err)
PL_TABLE = [
    ({"EnvironmentVariables": {"POSTBOX_NATS_URL": "nats://127.0.0.1:5001"}}, [5001]), ({"EnvironmentVariables": {"POSTBOX_NATS_URL": "nats://localhost:5002"}}, [5002]),
    ({"EnvironmentVariables": {"POSTBOX_NATS_URL": "nats://127.0.0.1"}}, [4222]), ({"EnvironmentVariables": {"POSTBOX_NATS_URL": "nats://far.example:5003"}}, []),
    ({"ProgramArguments": ["/x/nats-server", "--port", "5004"]}, [5004]), ({"ProgramArguments": ["/x/nats-server", "--port=5005"]}, [5005]),
    ({"ProgramArguments": ["/x/nats-server", "-p=5006"]}, [5006]), ({"ProgramArguments": ["/x/nats-server", "-c", "/x.conf"]}, []),
    ({"ProgramArguments": ["/x/nats-server", "-p", "99999"]}, []), ({"ProgramArguments": ["/bin/echo", "-p", "5007"]}, []), ({"ProgramArguments": "nats-server -p 5008"}, []), ("not a dict", []), ({}, []),
]
code, out, err = child(f"import _guard, json\nprint(json.dumps([[sorted(_guard._plist_ports(d)), want] for d, want in {PL_TABLE!r}]))")
rows = json.loads(out) if code == 0 and out else []
check(f"all {len(PL_TABLE)} job-file shapes give the right ports", len(rows) == len(PL_TABLE) and all(a == b for a, b in rows), f"{code} {rows} {err}")
time.sleep(0.3)
check("the stand-in listener heard nothing from the detection cases", heard == [], str(heard))

stand_in.close()
shutil.rmtree(WORK, ignore_errors=True)

print("the suites that import the guard are the ones the docs say, and a new suite cannot skip it unnoticed")
import ast   # noqa: E402
NO_GUARD_IMPORT = {"test_env_guard.py", "test_scrub_check.py"}     # see README "Test guard", last bullet: this suite runs guarded children; the other is a static scanner


def imports_guard(path):
    return any((isinstance(n, ast.Import) and any(a.name.split(".")[0] in ("_env", "_guard") for a in n.names))
               or (isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in ("_env", "_guard"))
               for n in ast.parse(path.read_text()).body)


SUITES = sorted(TESTS.glob("test_*.py"))
guarded = [f.name for f in SUITES if imports_guard(f)]
unguarded = [f.name for f in SUITES if not imports_guard(f)]
check("every suite imports _env or _guard at module level, except the two named ones", set(unguarded) == NO_GUARD_IMPORT, str(unguarded))
COUNT_TEXT = f"{len(guarded)} of the {len(SUITES)} suites"
for doc in ("README.md", "AGENTS.md", "CONTRIBUTING.md"):
    check(f"{doc} says {COUNT_TEXT} import the guard", COUNT_TEXT in (TESTS.parent / doc).read_text(), COUNT_TEXT)

print("no suite listens on or serves the default port itself")
BIND_4222 = re.compile(r"\b(bind|create_server|start_server|serve|listen)\(.*\b4222\b")
offenders = [f"{f.name}:{n}" for f in sorted(TESTS.glob("*.py")) for n, line in enumerate(f.read_text().splitlines(), 1) if BIND_4222.search(line)]
check("no file under tests/ binds, listens on or serves port 4222", offenders == [], str(offenders))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
