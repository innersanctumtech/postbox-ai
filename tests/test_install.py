"""The installer must render per-machine files from where it runs (nothing tied to one Mac), refuse to double-install,
be repeatable, and undo cleanly.

Every case runs in a throwaway HOME and a throwaway copy of the repo. The real launchd, the real services and the real
mailbox are never touched; the one live check starts its own mailbox on a spare port with its own data folder.
Run: python3 tests/test_install.py
"""
import ast
import contextlib
import io
import json
import os
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool (tests/test_telemetry.py covers telemetry itself)
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
import install  # noqa: E402

REAL_HOME = os.path.expanduser("~")
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sandbox(tmp, with_stub_nats=True):
    """A fake HOME plus a copy of the repo (no .venv, no mailbox data) and a stub nats-server."""
    home, repo, bindir = Path(tmp) / "home", Path(tmp) / "postbox", Path(tmp) / "bin"
    home.mkdir()
    bindir.mkdir()
    shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".venv", "jetstream", "__pycache__", ".git"))
    stub = bindir / "nats-server"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    # PYTHONDONTWRITEBYTECODE: Apple's system python writes its bytecode cache under $HOME, and that would look like "the installer changed something"
    env = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", "POSTBOX_STORE_DIR": str(Path(tmp) / "store"), "PYTHONDONTWRITEBYTECODE": "1",
           "POSTBOX_DOORS": _env.ALL_DOORS_ENV}   # every door on, so the Codex skill and relay checks below run; the shipped default (Claude only) is checked in test_doors.py
    if with_stub_nats:
        env["NATS_SERVER_BIN"] = str(stub)
    return home, repo, env


def run(repo, env, *args):
    r = subprocess.run([sys.executable, str(repo / "install.py"), *args], env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def tree(path):
    return sorted(str(p.relative_to(path)) for p in Path(path).rglob("*"))


# ---- what gets written (pure functions)
conf = install.nats_conf("postbox-x", 4299, '/tmp/we"ird\\dir')
check("NATS config carries the port and a safely quoted data folder", "listen: 127.0.0.1:4299" in conf and 'store_dir: "/tmp/we\\"ird\\\\dir"' in conf and "127.0.0.1" in conf.split("listen:")[1].split("\n")[0])
plist = plistlib.loads(install.relay_plist("l", "/py", "/relay.py", "/r&<>x", "/a:/b", "nats://127.0.0.1:4299", "/log"))
check("a path with & < > survives into a valid plist", plist["WorkingDirectory"] == "/r&<>x" and plist["EnvironmentVariables"]["POSTBOX_NATS_URL"] == "nats://127.0.0.1:4299")
plist = plistlib.loads(install.relay_plist("l", "/py", "/relay.py", "/r", "/a:/b", None, "/log"))
check("default port: the relay gets no NATS override", "POSTBOX_NATS_URL" not in plist["EnvironmentVariables"])
plist = plistlib.loads(install.nats_plist("l", "/bin/nats-server", "/c.conf", "/r", "/log"))
check("NATS service restarts itself and runs the given config", plist["KeepAlive"] is True and plist["ProgramArguments"] == ["/bin/nats-server", "-c", "/c.conf"])
check("installer source is valid Python 3.9 syntax", ast.parse((ROOT / "install.py").read_text(), feature_version=(3, 9)) is not None)

if sys.platform != "darwin":
    print("  skip installer runs (macOS only)")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

# ---- dry run
with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    code, out = run(repo, env, "--dry-run", "--skip-deps", "--port", str(free_port()))
    check("dry run changes nothing and says what it would do", code == 0 and tree(home) == [] and "com.postbox.nats.plist" in out and "DRY RUN" in out, out[-300:])

# ---- a real (sandboxed) install
with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    port = free_port()
    code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(port))
    agents = home / "Library" / "LaunchAgents"
    files = [agents / "com.postbox.nats.plist", agents / "com.postbox.relay.plist", home / "Library" / "Application Support" / "postbox" / "nats-server.conf"]
    check("install writes both services and the NATS config", code == 0 and all(f.is_file() for f in files), out[-400:])
    lint = [subprocess.run(["plutil", "-lint", str(f)], capture_output=True, text=True).returncode for f in files[:2]]
    check("both launchd files pass plutil", lint == [0, 0], str(lint))
    rendered = "".join(f.read_text() for f in files)
    check("nothing from the real user's home is baked in", REAL_HOME not in rendered, REAL_HOME)
    check("the files point at THIS copy of the repo, this home and this port", str(repo) in rendered and str(home) in rendered and f"127.0.0.1:{port}" in rendered)
    link = home / ".local" / "bin" / "post"
    check("the `post` command is linked to this copy", link.is_symlink() and os.path.realpath(link) == os.path.realpath(repo / "bin" / "post"))
    check("a home without ~/.local/bin on PATH gets told how to fix it", "is not on this shell's PATH" in out)
    first = {f.name: f.read_bytes() for f in files}
    code2, out2 = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(port))
    check("running it again repairs in place and writes identical files", code2 == 0 and first == {f.name: f.read_bytes() for f in files} and "already correct" in out2, out2[-300:])
    code3, out3 = run(repo, env, "--uninstall", "--no-launchd", "--port", str(port))
    check("uninstall removes the services and the link but keeps the code and data", code3 == 0 and not any(f.exists() for f in files[:2]) and not link.exists() and (repo / "install.py").exists(), out3[-300:])

# ---- refusals: never disturb another post box, never fail silently
with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "com.example.postbox.plist").write_text("<plist/>")
    code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(free_port()))
    check("another post box on this Mac: refuses and changes nothing", code == 2 and "another post box install exists" in out and not (home / "Library" / "Application Support").exists(), out[-300:])
    code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(free_port()), "--force")
    check("--force overrides that refusal", code == 0, out[-300:])

with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(held.getsockname()[1]))
    held.close()
    check("a port that is already answering: refuses and changes nothing", code == 2 and "already listens" in out and tree(home) == [], out[-300:])

with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp, with_stub_nats=False)
    env["PATH"] = "/usr/bin:/bin"
    env["NATS_SERVER_BIN"] = str(Path(tmp) / "does-not-exist" / "nats-server")
    code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(free_port()))
    check("no nats-server: a clear error, not a crash", code == 2 and "nats-server not found" in out and "Traceback" not in out, out[-300:])

# ---- the login key is never silently dropped: a config WITHOUT a login only in a bare test setup
NO_KEYLIB = tempfile.mkdtemp()    # holds a fake `nkeys` that cannot be imported: stands for "no python here can load the key library"
(Path(NO_KEYLIB) / "nkeys").mkdir()
(Path(NO_KEYLIB) / "nkeys" / "__init__.py").write_text('raise ImportError("hidden for this test")\n')
HAVE_KEYLIB = subprocess.run([sys.executable, "-c", "import nkeys"], capture_output=True).returncode == 0
SUPPORT_REL = Path("Library") / "Application Support" / "postbox"


def without_keylib(env):
    return dict(env, PYTHONPATH=NO_KEYLIB)


with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    code, out = run(repo, without_keylib(env), "--skip-deps", "--no-launchd", "--port", str(free_port()))
    conf_file = home / SUPPORT_REL / "nats-server.conf"
    check("a bare test setup (no key library, no key yet, no linked Macs, --skip-deps) still gets a config, written without a login and saying so",
          code == 0 and conf_file.is_file() and "authorization" not in conf_file.read_text() and "WITHOUT a login" in out, out[-300:])

with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    (home / SUPPORT_REL).mkdir(parents=True)
    (home / SUPPORT_REL / "machine.seed").write_text("placeholder: the installer must stop before it reads this\n")
    before = tree(home)
    code, out = run(repo, without_keylib(env), "--skip-deps", "--no-launchd", "--port", str(free_port()))
    check("this Mac already has a login key but no python can load the key library: refuses in plain words and changes nothing",
          code == 2 and "login key" in out and "nkeys" in out and "nothing was changed" in out and "Traceback" not in out and "WITHOUT" not in out and tree(home) == before, out[-400:])

with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    (home / SUPPORT_REL).mkdir(parents=True)
    (home / SUPPORT_REL / "link.json").write_text(json.dumps({"machine": "alpha", "peers": {"beta": {"host": "beta.example.invalid", "port": free_port()}}}))
    before = tree(home)
    code, out = run(repo, without_keylib(env), "--skip-deps", "--no-launchd", "--port", str(free_port()))
    check("other Macs are linked but no python can load the key library: refuses, names the linked Mac, changes nothing (an open mailbox would be reachable over Tailscale)",
          code == 2 and "beta" in out and "nkeys" in out and "nothing was changed" in out and "Traceback" not in out and "WITHOUT" not in out and tree(home) == before, out[-400:])

with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    never = Path(tmp) / "bin" / "tailscale-never-run"      # a Tailscale that must not be asked anything: the peer is given by name, so no lookup is needed
    never.write_text("#!/bin/sh\nexit 99\n")
    never.chmod(0o755)
    env["TAILSCALE_BIN"] = str(never)
    before = tree(home)
    code, out = run(repo, without_keylib(env), "--skip-deps", "--no-launchd", "--port", str(free_port()), "--link", "--peer", "beta=beta.example.invalid")
    check("--link --peer on the command line and no key library: refused too (a peer named now is a linked Mac), nothing is written",
          code == 2 and "beta" in out and "nkeys" in out and "nothing was changed" in out and "Traceback" not in out and "WITHOUT" not in out and tree(home) == before, out[-400:])

if HAVE_KEYLIB:
    with tempfile.TemporaryDirectory() as tmp:
        home, repo, env = sandbox(tmp)
        port = free_port()
        code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(port))
        conf_file = home / SUPPORT_REL / "nats-server.conf"
        installed = conf_file.read_bytes() if conf_file.exists() else b""
        code2, out2 = run(repo, without_keylib(env), "--skip-deps", "--no-launchd", "--port", str(port))
        check("a finished install, re-run with a python that has no key library: refused, and the working login config is left byte for byte as it was",
              code == 0 and b"authorization" in installed and code2 == 2 and conf_file.read_bytes() == installed, out2[-400:])
else:
    print("  skip re-run check (this python has no nkeys; run the tests with the repo's .venv)")

# ---- Codex skill
with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    run(repo, env, "--skip-deps", "--no-launchd", "--port", str(free_port()))
    check("no ~/.codex: no Codex skill installed", not (home / ".codex").exists())
with tempfile.TemporaryDirectory() as tmp:
    home, repo, env = sandbox(tmp)
    (home / ".codex" / "skills").mkdir(parents=True)
    run(repo, env, "--skip-deps", "--no-launchd", "--port", str(free_port()))
    skill = home / ".codex" / "skills" / "post" / "SKILL.md"
    check("~/.codex present: the Codex skill is installed", skill.is_file() and skill.read_bytes() == (ROOT / "codex-skill" / "post" / "SKILL.md").read_bytes())
    skill.write_text("someone's own edit")
    run(repo, env, "--skip-deps", "--no-launchd", "--port", str(free_port()), "--force")
    check("an existing, different Codex skill is left alone", skill.read_text() == "someone's own edit")

# ---- helpers for the checks below
@contextlib.contextmanager
def env_set(**values):
    """Set environment variables in THIS process for a block (the installer's functions read them at call time)."""
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def call(fn, *args):
    """(returned value, exit code or None, stderr text) of calling an installer function."""
    err = io.StringIO()
    code = None
    value = None
    with contextlib.redirect_stderr(err):
        try:
            value = fn(*args)
        except SystemExit as exc:
            code = exc.code
    return value, code, err.getvalue()


# ---- launchd reload: wait for the old service to stop, never blame SSH wrongly, always show the real error
STUB = """#!/bin/bash
S="$STUB_STATE"; echo "$*" >> "$S/calls.log"
label() { basename "${1##*/}" .plist; }
case "$1" in
  print)   l="${2##*/}"; st=$(cat "$S/$l.state" 2>/dev/null || echo unloaded)
           if [ "$st" = loaded ]; then exit 0; fi
           if [ "$st" = unloading ]; then n=$(cat "$S/$l.n" 2>/dev/null || echo 0)
             if [ "$n" -gt 0 ]; then echo $((n-1)) > "$S/$l.n"; exit 0; fi
             echo unloaded > "$S/$l.state"; exit 113; fi
           exit 113 ;;
  bootout) l=$(label "$3"); echo unloading > "$S/$l.state"; echo "${STUB_UNLOAD_POLLS:-2}" > "$S/$l.n"; exit 0 ;;
  bootstrap) l=$(label "$3"); st=$(cat "$S/$l.state" 2>/dev/null || echo unloaded)
           if [ -n "${STUB_BOOTSTRAP_FAILS:-}" ] || [ "$st" != unloaded ]; then
             echo "bootstrap-failed" >> "$S/calls.log"; echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
           echo loaded > "$S/$l.state"; exit 0 ;;
esac
exit 0
"""


def launchd_case(initial_state, **extra):
    tmp = tempfile.mkdtemp()
    stubdir = Path(tmp) / "stub"
    stubdir.mkdir()
    (stubdir / "launchctl").write_text(STUB)
    (stubdir / "launchctl").chmod(0o755)
    if initial_state:
        (Path(tmp) / "t.relay.state").write_text(initial_state)
    env = {"STUB_STATE": tmp, "PATH": f"{stubdir}:{os.environ['PATH']}", "POSTBOX_LAUNCHD_RETRY_WAIT": "0", "POSTBOX_LAUNCHD_STOP_WAIT": "20"}
    env.update(extra)
    with env_set(**env):
        result = call(install.load_service, "t.relay", Path("/x/t.relay.plist"))
    calls = (Path(tmp) / "calls.log").read_text().splitlines()
    shutil.rmtree(tmp, ignore_errors=True)
    return result, calls


(_, code, _), calls = launchd_case("loaded")
verbs = [c.split()[0] for c in calls]
check("re-run: the old service is stopped, waited for, then loaded (no early bootstrap)", code is None and "bootstrap-failed" not in calls and verbs.index("bootout") < verbs.index("enable") < verbs.index("bootstrap") < verbs.index("kickstart"), str(calls))
between = verbs[verbs.index("bootout") + 1:verbs.index("bootstrap")]
check("it polled until the old service was really gone", between.count("print") >= 3, str(between))
(_, code, _), calls = launchd_case(None)
check("first install: no bootout, just enable, bootstrap, kickstart", code is None and [c.split()[0] for c in calls if c.split()[0] != "print"] == ["enable", "bootstrap", "kickstart"], str(calls))
(_, code, err), calls = launchd_case("unloaded", STUB_BOOTSTRAP_FAILS="1")
check("a load that keeps failing: exit 2 with launchctl's own words, 3 tries, no SSH blame", code == 2 and "Input/output error" in err and "after 3 tries" in err and "SSH" not in err and calls.count("bootstrap-failed") == 3, err + str(calls))
(_, code, err), _ = launchd_case("unloaded", STUB_BOOTSTRAP_FAILS="1", SSH_CONNECTION="1.2.3.4 5 6.7.8.9 22")
check("over SSH the same failure adds the SSH hint", code == 2 and "SSH" in err and "Input/output error" in err, err)
(_, code, err), _ = launchd_case("loaded", STUB_UNLOAD_POLLS="100000", POSTBOX_LAUNCHD_STOP_WAIT="1")
check("an old service that never stops: a clear error, no bootstrap attempted", code == 2 and "did not stop within" in err, err)

# ---- relay liveness (used by --check and right after install): recent life signs, never an old relay_ready
LOG_TMP = tempfile.mkdtemp()
(Path(LOG_TMP) / "Library" / "Logs").mkdir(parents=True)
RELAY_LOG = Path(LOG_TMP) / "Library" / "Logs" / "postbox-relay.log"


def relay_log(*rows, raw=()):
    now = time.time()
    lines = []
    for age, event in rows:
        stamp = datetime.fromtimestamp(now - age).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        lines.append(json.dumps({"t": stamp, "event": event}))
    RELAY_LOG.write_text("\n".join(list(raw) + lines) + "\n")


def alive(since=0):
    with env_set(HOME=LOG_TMP):
        return install.relay_alive(since)


if RELAY_LOG.exists():
    RELAY_LOG.unlink()
check("no relay log: not alive", alive() is False)
relay_log((3 * 3600, "relay_ready"))
check("--check: an OLD relay_ready is not a running relay", alive() is False)
relay_log((60, "heartbeat"))
check("--check: a heartbeat a minute ago is a running relay", alive() is True)
relay_log((20 * 60, "heartbeat"), (60, "relay_error"))
check("errors are not life signs: a 20 min old heartbeat plus a fresh error is not alive", alive() is False)
relay_log((60, "heartbeat"))
check("right after an install a relay_ready NEWER than the install is required", alive(since=time.time() - 10) is False)
relay_log((5, "relay_ready"))
check("...and a fresh relay_ready satisfies it", alive(since=time.time() - 30) is True)
relay_log((60, "heartbeat"), raw=("123", "null", "[]", "{not json", '{"t": "garbage", "event": "x"}'))
check("bare numbers, null, lists and torn lines in the log never crash the check", alive() is True)
shutil.rmtree(LOG_TMP, ignore_errors=True)

# ---- python environment health, Homebrew lookup, real Python 3.9
with tempfile.TemporaryDirectory() as tmp:
    broken = Path(tmp) / "python"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    check("a python that cannot import the mailbox library is reported broken", install.venv_healthy(broken) is False)
    check("a missing python is reported broken, not a crash", install.venv_healthy(Path(tmp) / "nope") is False)
real_venv = ROOT / ".venv" / "bin" / "python"
if real_venv.exists():
    check("this repo's own environment is reported healthy", install.venv_healthy(real_venv) is True)
known = [c for c in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.access(c, os.X_OK)]
if known:
    with env_set(PATH="/usr/bin:/bin"):
        check("Homebrew is found even when it is not on PATH", install.find_brew() == known[0], str(install.find_brew()))
system_python = "/usr/bin/python3"
if os.access(system_python, os.X_OK) and subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0:
    r = subprocess.run([system_python, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import install; assert install.nats_conf('x', 1, '/a'); print(sys.version_info[:2])"], capture_output=True, text=True, timeout=60)
    check(f"the installer imports and runs under the system Python ({r.stdout.strip()})", r.returncode == 0, r.stderr[-300:])
else:
    print("  skip system Python check (not present or no developer tools)")

# ---- live: the rendered config really runs a mailbox (own port, own data folder, real nats-server and relay)
nats_bin = install.find_nats()
venv_python = ROOT / ".venv" / "bin" / "python"
if not nats_bin or not venv_python.exists():
    print("  skip live mailbox check (needs nats-server and this repo's .venv)")
else:
    with tempfile.TemporaryDirectory() as tmp:
        home, repo, env = sandbox(tmp)
        env["NATS_SERVER_BIN"] = nats_bin
        port = free_port()
        code, out = run(repo, env, "--skip-deps", "--no-launchd", "--port", str(port))
        conf_path = home / "Library" / "Application Support" / "postbox" / "nats-server.conf"
        procs = []
        try:
            (Path(tmp) / "store").mkdir(exist_ok=True)
            procs.append(subprocess.Popen([nats_bin, "-c", str(conf_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            end = time.time() + 15
            while time.time() < end and not install.port_in_use(port):
                time.sleep(0.3)
            check("the rendered NATS config starts a real server on its port", code == 0 and install.port_in_use(port))
            # the rendered config demands a login, so the test's clients use the key the installer made in the sandbox
            live_env = dict(os.environ, POSTBOX_NATS_URL=f"nats://127.0.0.1:{port}", POSTBOX_SUPPORT_DIR=str(home / "Library" / "Application Support" / "postbox"))
            relay_log = open(Path(tmp) / "relay.log", "w")
            procs.append(subprocess.Popen([str(venv_python), str(ROOT / "postbox" / "relay.py")], env=live_env, stdout=relay_log, stderr=subprocess.STDOUT))
            end = time.time() + 20
            while time.time() < end and "relay_ready" not in (Path(tmp) / "relay.log").read_text():
                time.sleep(0.3)
            check("the relay connects to that mailbox and reports ready", "relay_ready" in (Path(tmp) / "relay.log").read_text())
            who = subprocess.run([str(ROOT / "bin" / "post"), "who"], env=live_env, capture_output=True, text=True, timeout=30)
            check("`post who` answers through it", who.returncode == 0 and "Reachable now" in who.stdout, who.stdout + who.stderr)
            ghost = f"pbinstalltest{port}"
            sent_env = dict(live_env, POSTBOX_AS=f"claude@{ghost}")
            sent = subprocess.run([str(ROOT / "bin" / "post"), f"claude@{ghost}", "installer live check, please ignore"], env=sent_env, capture_output=True, text=True, timeout=30)
            check("a message makes the full trip (post to mailbox to relay and back) and is answered", sent.returncode == 3 and sent.stdout.startswith("queued: nobody reachable"), sent.stdout + sent.stderr)
            venv_env = live_env
            ok_run = subprocess.run([str(venv_python), str(ROOT / "postbox" / "selfcheck.py")], env=venv_env, capture_output=True, text=True, timeout=60)
            check("selfcheck: a real round trip passes while everything runs", ok_run.returncode == 0 and "selfcheck ok" in ok_run.stdout, ok_run.stdout + ok_run.stderr)
            procs[1].terminate()
            procs[1].wait(timeout=10)
            no_relay = subprocess.run([str(venv_python), str(ROOT / "postbox" / "selfcheck.py")], env=venv_env, capture_output=True, text=True, timeout=60)
            check("selfcheck: with the relay stopped it fails and says the relay never answered", no_relay.returncode == 1 and "relay never answered" in no_relay.stderr, no_relay.stderr)
            procs[0].terminate()
            procs[0].wait(timeout=10)
            no_nats = subprocess.run([str(venv_python), str(ROOT / "postbox" / "selfcheck.py")], env=venv_env, capture_output=True, text=True, timeout=60)
            check("selfcheck: with the mailbox stopped it fails and says it cannot reach it", no_nats.returncode == 1 and "cannot reach the mailbox" in no_nats.stderr, no_nats.stderr)
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()

shutil.rmtree(NO_KEYLIB, ignore_errors=True)
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
