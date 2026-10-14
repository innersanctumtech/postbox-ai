"""Cross-machine tests: two emulated Macs (each with its own real mailbox, real relay and fake Claude sessions) message each other
through the real login and the real per-peer permissions. A stub stands in for the Tailscale command. Nothing here touches this
Mac's real post box, launchd or Tailscale.

Run: .venv/bin/python tests/test_link.py
"""
import asyncio
import contextlib
import io
import json
import os
os.environ["POSTBOX_CODEX_WATCH"] = os.path.join(__import__("tempfile").mkdtemp(prefix="postbox-watch-"), "codex-watch.json")   # never the real watch list
os.environ["POSTBOX_TELEMETRY"] = "0"   # tests never write telemetry events into the real spool (tests/test_telemetry.py covers telemetry itself)
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
sys.path.insert(0, str(ROOT / "postbox"))
import install  # noqa: E402
import lib  # noqa: E402

# lib.my_machine() falls back to this host's name, so a check such as "a machine address is never misrouted" would fail on a Mac that
# happens to be called mac-b. Pin the name: no check below may depend on the host it runs on. (The emulated Macs pop it from their own
# environment, and each takes its name from its own link.json.)
os.environ["POSTBOX_MACHINE"] = "alpha"

PY = sys.executable
POST, RELAY, SELFCHECK, KEYTOOL = (str(ROOT / "postbox" / n) for n in ("post.py", "relay.py", "selfcheck.py", "keytool.py"))
NATS_BIN = install.find_nats()
PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def raises(label, fn, kinds=(ValueError, SystemExit)):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
    except kinds:
        check(label, True)
        return
    check(label, False, "(no error raised)")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(fn, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


@contextlib.contextmanager
def env_set(**values):
    """Set environment variables in THIS process for a block (the installer's functions read them when they are called)."""
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


def run_fn(fn, *args):
    """Call fn(*args) with stdout and stderr going into ONE buffer, so the ORDER of the lines can be checked.
    -> (return value, exit code or None, text). An exception that is not a clean exit comes back as the code 'TRACEBACK <type>: <message>':
    a traceback in front of a user is exactly what these checks exist to catch."""
    buf = io.StringIO()
    value = code = None
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            value = fn(*args)
        except SystemExit as exc:
            code = exc.code
        except Exception as exc:  # noqa: BLE001
            code = f"TRACEBACK {type(exc).__name__}: {exc}"
    return value, code, buf.getvalue()


def said_by(fn):
    """Call fn -> (kind, text). kind None: it returned normally. 'plain': it stopped with an installer sentence (a clean exit, or a
    TailscaleError whose message is the sentence). 'crash': any other exception, which a user would see as a traceback."""
    buf = io.StringIO()
    plain = getattr(install, "TailscaleError", ())
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            fn()
        except SystemExit:
            return "plain", buf.getvalue()
        except plain as exc:
            return "plain", str(exc)
        except Exception as exc:  # noqa: BLE001
            return "crash", f"{type(exc).__name__}: {exc}"
    return None, buf.getvalue()


def key_before_error(text, key):
    """The PUBLIC KEY line was printed, and it came BEFORE the first ERROR line."""
    return bool(install.PUBLIC_KEY_RE.match(key)) and "PUBLIC KEY" in text and key in text and "ERROR" in text and text.index(key) < text.index("ERROR")


# ------------------------------------------------------------------ addresses
print("addresses")
check("bare agent stays in my project", lib.parse_target("claude", "proj", "alpha") == ("claude", "proj", None))
check("agent@project stays on this Mac", lib.parse_target("codex@project-b", "proj", "alpha") == ("codex", "project-b", None))
check("agent@project@machine names another Mac", lib.parse_target("claude@dotclaude@mac-b", "p", "alpha") == ("claude", "dotclaude", "mac-b"))
check("claude@@mac-b means my own project on the other Mac", lib.parse_target("claude@@mac-b", "mine", "alpha") == ("claude", "mine", "mac-b"))
check("the machine that is this Mac counts as local", lib.parse_target("claude@x@alpha", "p", "alpha") == ("claude", "x", None))
raises("a trailing @ with no machine is refused", lambda: lib.parse_target("claude@x@", "p", "alpha"))
raises("four parts are refused", lambda: lib.parse_target("a@b@c@d", "p", "alpha"))
raises("no agent is refused", lambda: lib.parse_target("@x@y", "p", "alpha"))
raises("old callers can never misroute a machine address", lambda: lib.parse_address("claude@x@mac-b", "p"))
check("a login key never shows when a mailbox target is printed or logged", "SUSECRETSEED" not in repr(lib.Target("nats://x:1", "SUSECRETSEED", "x")) and lib.Target("nats://x:1", "SUSECRETSEED", "x").auth() == {"nkeys_seed_str": "SUSECRETSEED"})

# ------------------------------------------------------------------ the mailbox config
print("mailbox config")
with tempfile.TemporaryDirectory() as kt:
    seed_file = Path(kt) / "s" / "machine.seed"
    first = subprocess.run([PY, KEYTOOL, "ensure", str(seed_file)], capture_output=True, text=True)
    second = subprocess.run([PY, KEYTOOL, "ensure", str(seed_file)], capture_output=True, text=True)
    pub_again = subprocess.run([PY, KEYTOOL, "pub", str(seed_file)], capture_output=True, text=True)
    key = first.stdout.strip()
    check("keytool makes a public key and keeps it stable", bool(install.PUBLIC_KEY_RE.match(key)) and second.stdout.strip() == key == pub_again.stdout.strip())
    check("the seed file is private (mode 600) and never printed", stat.S_IMODE(seed_file.stat().st_mode) == 0o600 and seed_file.read_text().strip() not in first.stdout + first.stderr)
    check("keytool check accepts a real key and refuses junk", subprocess.run([PY, KEYTOOL, "check", key]).returncode == 0 and subprocess.run([PY, KEYTOOL, "check", "UNOTAKEY"]).returncode == 1)
    missing = subprocess.run([PY, KEYTOOL, "pub", str(Path(kt) / "nope")], capture_output=True, text=True)
    check("keytool pub on a missing file fails in one plain line", missing.returncode == 2 and "cannot use the key file" in missing.stderr and "Traceback" not in missing.stderr)
    local_pub = key
    other_pub = subprocess.run([PY, KEYTOOL, "ensure", str(Path(kt) / "o" / "machine.seed")], capture_output=True, text=True).stdout.strip()

conf = install.nats_conf("postbox-x", 4299, "/tmp/s", local_pub, [("mac-b", other_pub)])
check("with a key: every client must log in, and both keys are listed", "authorization {" in conf and f"nkey: {local_pub}" in conf and f"nkey: {other_pub}" in conf)
peer_line = [ln for ln in conf.splitlines() if other_pub in ln][0]
local_line = [ln for ln in conf.splitlines() if local_pub in ln][0]
check("a peer's key may only publish post.> and who.> and only hear its OWN inbox name (never _INBOX.>); this Mac's key has no limits",
      'publish: { allow: ["post.>", "who.>"] }' in peer_line
      and f'subscribe: {{ allow: ["{lib.peer_inbox_prefix(other_pub)}.>"] }}' in peer_line
      and "_INBOX.>" not in peer_line and "permissions" not in local_line, peer_line)
check("the mailbox still listens on 127.0.0.1 only", "listen: 127.0.0.1:4299" in conf and "0.0.0.0" not in conf)
check("without a key (test setups) there is no authorization block", "authorization" not in install.nats_conf("x", 4299, "/s"))
raises("a peer name that could inject config is refused", lambda: install.nats_conf("x", 4299, "/s", local_pub, [("x\n}", other_pub)]))
raises("a malformed peer key is refused", lambda: install.nats_conf("x", 4299, "/s", local_pub, [("mac-b", "UBAD")]))
if NATS_BIN:
    with tempfile.TemporaryDirectory() as td:
        os.environ["POSTBOX_SUPPORT_DIR"] = td
        check("nats-server itself accepts the rendered config (nats-server -t)", install.validate_conf(NATS_BIN, conf) is True)
        _, bad_code, bad_text = run_fn(install.validate_conf, NATS_BIN, "authorization {", "the public key in peers/zed.pub", "Fix it like this.")
        check("a rejected config stops with the label it was given and the fix, and changes nothing",
              bad_code == 2 and "rejected the public key in peers/zed.pub" in bad_text and "Fix it like this." in bad_text, f"{bad_code} {bad_text}")
        raises("and the check CAN fail: a broken config stops the install before anything changes", lambda: install.validate_conf(NATS_BIN, "authorization {"))
        check("the check leaves no temp file behind", not list(Path(td).glob("*.check")))
        del os.environ["POSTBOX_SUPPORT_DIR"]

# ------------------------------------------------------------------ Tailscale steps against a stub
print("Tailscale steps (stub)")
STUB_TS = r'''
import json, os, sys
d = os.environ["STUB_DIR"]
state, calls = os.path.join(d, "serve.json"), os.path.join(d, "calls.log")
def flag(name):
    return os.path.exists(os.path.join(d, name))
def load():
    try:
        return json.load(open(state))
    except OSError:
        return {}
args = sys.argv[1:]
open(calls, "a").write(" ".join(args) + "\n")
if flag("down"):
    print("failed to connect to local Tailscale service; is Tailscale running?", file=sys.stderr); sys.exit(1)
if args[:3] == ["serve", "status", "--json"]:
    tcp = load()
    print(json.dumps({"TCP": {p: {"TCPForward": t} for p, t in tcp.items()}} if tcp else {})); sys.exit(0)
if args[:2] == ["serve", "--bg"] and "--tcp" in args:
    if flag("serve_fails"):
        print("Serve is not enabled on your tailnet.", file=sys.stderr); sys.exit(1)
    tcp = load(); tcp[args[args.index("--tcp") + 1]] = args[-1].replace("tcp://", ""); json.dump(tcp, open(state, "w"))
    print("Available within your tailnet:"); sys.exit(0)
if args[:1] == ["serve"] and args[-1] == "off" and any(a.startswith("--tcp=") for a in args):
    if flag("off_fails"):
        print("serve: could not remove the forward", file=sys.stderr); sys.exit(1)
    tcp = load(); tcp.pop([a for a in args if a.startswith("--tcp=")][0].split("=")[1], None); json.dump(tcp, open(state, "w")); sys.exit(0)
if args[:2] == ["status", "--json"]:
    print(open(os.path.join(d, "status.json")).read()); sys.exit(0)
print("stub tailscale: unexpected " + repr(args), file=sys.stderr); sys.exit(2)
'''


def device(host, dns, online):
    return {"HostName": host, "DNSName": dns + ".", "Online": online, "OS": "macOS"}


def make_stub(folder, devices, state="Running"):
    """A stand-in for the tailscale command. `state` is the BackendState that `tailscale status --json` reports. Marker files in the
    folder make it misbehave: down (every command fails, as when Tailscale is not running), serve_fails, off_fails."""
    folder.mkdir(parents=True, exist_ok=True)
    stub = folder / "tailscale"
    stub.write_text(f"#!{PY}\n" + STUB_TS)
    stub.chmod(0o755)
    (folder / "status.json").write_text(json.dumps({"BackendState": state, "Peer": {f"k{i}": d for i, d in enumerate(devices)}}))
    return stub


def stub_calls(folder, word):
    try:
        return sum(1 for ln in (folder / "calls.log").read_text().splitlines() if word in ln)
    except OSError:
        return 0


with tempfile.TemporaryDirectory() as tmp:
    sd = Path(tmp) / "ts"
    stub = str(make_stub(sd, [device("localhost", "iphone.x.ts.net", False), device("Mac-B", "mac-b-jarvis.x.ts.net", False),
                              device("Mac-B", "mac-b-user.x.ts.net", True)]))
    os.environ["STUB_DIR"] = str(sd)
    with contextlib.redirect_stdout(io.StringIO()):
        install.ensure_serve(stub, 4301)
        install.ensure_serve(stub, 4301)
    check("the forward is created once and a second run changes nothing", install.serve_forward(stub, 4301) == "127.0.0.1:4301" and stub_calls(sd, "serve --bg") == 1)
    (sd / "serve.json").write_text(json.dumps({"4302": "127.0.0.1:9999"}))
    raises("someone else's forward on that port is never overwritten", lambda: install.ensure_serve(stub, 4302))
    check("...and it is left exactly as it was", install.serve_forward(stub, 4302) == "127.0.0.1:9999")
    (sd / "serve.json").write_text(json.dumps({"4301": "127.0.0.1:4301", "4302": "127.0.0.1:9999"}))
    with contextlib.redirect_stdout(io.StringIO()):
        install.remove_serve(stub, 4301)
        install.remove_serve(stub, 4302)
    check("uninstall removes OUR forward only", install.serve_forward(stub, 4301) is None and install.serve_forward(stub, 4302) == "127.0.0.1:9999")
    check("of two devices called Mac-B the online one is chosen", install.resolve_peer_host(stub, "mac-b") == "mac-b-user.x.ts.net")
    make_stub(sd, [device("Mac-B", "a.x.ts.net", True), device("Mac-B", "b.x.ts.net", True)])
    raises("two online devices of the same name: it refuses to guess", lambda: install.resolve_peer_host(stub, "mac-b"))
    make_stub(sd, [device("Mac-A", "mac-a.x.ts.net", True)])
    raises("no device of that name: it says so instead of guessing", lambda: install.resolve_peer_host(stub, "mac-b"))
    check("a single device is found even when offline", install.resolve_peer_host(stub, "mac-a") == "mac-a.x.ts.net")

    # a linked Mac that cannot be found: the message says how to look it up again (the same words `post` uses), never "matches: none"
    _, code, text = run_fn(install.resolve_peer_host, stub, "mac-b")
    check("no device of that name: the message says to run install.py --link --peer mac-b again to look it up",
          code == 2 and "--link --peer mac-b" in text and "again" in text and "matches: none" not in text, f"{code} {text}")
    make_stub(sd, [device("Mac-B", "a.x.ts.net", True), device("Mac-B", "b.x.ts.net", True)])
    _, code, text = run_fn(install.resolve_peer_host, stub, "mac-b")
    check("two devices of that name: the message names both and says how to choose one (--peer mac-b=<tailnet name>)",
          code == 2 and "a.x.ts.net" in text and "b.x.ts.net" in text and "--peer mac-b=" in text, f"{code} {text}")

    # Tailscale signed out or stopped: BackendState says so, and the answer is one plain sentence (never "matches: none", never a crash)
    tport = free_port()
    sd_state = Path(tmp) / "ts-state"
    for state in ("NeedsLogin", "Stopped"):
        stub_state = str(make_stub(sd_state, [device("Mac-B", "mac-b.x.ts.net", True)], state=state))
        with env_set(STUB_DIR=sd_state):
            for what, fn in (("looking up a linked Mac", lambda: install.resolve_peer_host(stub_state, "mac-b")),
                             ("opening the forward", lambda: install.ensure_serve(stub_state, tport))):
                kind, text = said_by(fn)
                check(f"Tailscale state {state}, {what}: one plain 'signed out / not running' sentence that names the state",
                      kind == "plain" and "signed out / not running on this Mac" in text and state in text and "matches" not in text, f"{kind}: {text}")
        check(f"Tailscale state {state}: no forward was attempted", stub_calls(sd_state, "serve") == 0)
    sd_down = Path(tmp) / "ts-down"
    stub_down = str(make_stub(sd_down, [device("Mac-B", "mac-b.x.ts.net", True)]))
    (sd_down / "down").write_text("")
    with env_set(STUB_DIR=sd_down):
        for what, fn in (("looking up a linked Mac", lambda: install.resolve_peer_host(stub_down, "mac-b")),
                         ("opening the forward", lambda: install.ensure_serve(stub_down, tport))):
            kind, text = said_by(fn)
            check(f"Tailscale not running at all, {what}: one plain sentence with the next step, not a traceback",
                  kind == "plain" and "not answering" in text and "Tailscale app" in text, f"{kind}: {text}")

    # uninstall must not claim success while the forward is still there
    rm_port = free_port()
    (sd / "serve.json").write_text(json.dumps({str(rm_port): f"127.0.0.1:{rm_port}"}))
    (sd / "off_fails").write_text("")
    done, _, text = run_fn(install.remove_serve, stub, rm_port)
    check("remove_serve: when Tailscale will not remove the forward it warns with the exact command and never says 'removed'",
          done is False and "WARN" in text and f"tailscale serve --tcp={rm_port} off" in text and "removed the Tailscale forward" not in text, f"{done} {text}")
    check("...and the forward really is still there", install.serve_forward(stub, rm_port) == f"127.0.0.1:{rm_port}")
    (sd / "off_fails").unlink()
    done, _, text = run_fn(install.remove_serve, stub, rm_port)
    check("remove_serve: says 'removed' only when the forward is really gone", done is True and "removed the Tailscale forward" in text and install.serve_forward(stub, rm_port) is None, f"{done} {text}")
    foreign_port = free_port()
    (sd / "serve.json").write_text(json.dumps({str(foreign_port): f"127.0.0.1:{free_port()}"}))
    done, _, text = run_fn(install.remove_serve, stub, foreign_port)
    check("remove_serve: someone else's forward is left alone and that is not a failure", done is True and "removed" not in text and install.serve_forward(stub, foreign_port) is not None, f"{done} {text}")
    with env_set(STUB_DIR=sd_down):
        done, _, text = run_fn(install.remove_serve, stub_down, rm_port)
    check("remove_serve: when Tailscale cannot even be asked it warns with the exact command and reports failure",
          done is False and "WARN" in text and f"tailscale serve --tcp={rm_port} off" in text, f"{done} {text}")

    # the odd ways a Tailscale command can go wrong are one plain sentence too (no OSError, TimeoutExpired or JSON traceback)
    kind, text = said_by(lambda: install.resolve_peer_host(str(Path(tmp) / "no-such-tailscale"), "mac-b"))
    check("a tailscale command that cannot be run at all: one plain sentence, not an OSError traceback",
          kind == "plain" and "could not run" in text and "Tailscale app" in text, f"{kind}: {text}")
    running = subprocess.CompletedProcess([], 0, stdout=json.dumps({"BackendState": "Running"}), stderr="")
    no_forwards = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
    with mock.patch.object(subprocess, "run", side_effect=[running, no_forwards, subprocess.TimeoutExpired("tailscale", 60)]):
        kind, text = said_by(lambda: install.ensure_serve("tailscale", tport))
    check("a `tailscale serve` that never finishes: one plain sentence with the command to run by hand, not a TimeoutExpired traceback",
          kind == "plain" and "did not finish" in text and "tailscale serve --bg --tcp" in text, f"{kind}: {text}")
    for what, answer in (("is not JSON", "this is not json"), ("is JSON but not an object", "[1, 2]")):
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=answer, stderr="")):
            kind, text = said_by(lambda: install.resolve_peer_host("tailscale", "mac-b"))
        check(f"a tailscale answer that {what}: one plain sentence, not a traceback", kind == "plain" and "expected JSON form" in text, f"{kind}: {text}")
    del os.environ["STUB_DIR"]

# ------------------------------------------------------------------ finishing a link: public key first, then Tailscale, then the peer tests
print("finishing a link (stub Tailscale, stub selfcheck)")
STUB_SELFCHECK = r'''
import os, sys
name = sys.argv[sys.argv.index("--peer") + 1]
with open(os.path.join(os.environ["STUB_DIR"], "selfchecks.log"), "a") as fh:
    fh.write(name + "\n")
code = int(os.environ.get("STUB_SC_" + name.upper(), "0"))
print("stub selfcheck: " + ("fine" if code == 0 else "exit " + str(code)), file=sys.stderr if code else sys.stdout)
sys.exit(code)
'''
ONE_PEER = {"beta": {"host": "beta.x.ts.net", "port": lib.DEFAULT_PORT}}


def link_case(state="Running", down=False, serve_fails=False, foreign=None, sc=None, publish=False, peers=ONE_PEER):
    """Run install.finish_link against a stub Tailscale and a stub selfcheck and report what happened. `sc` maps a peer name to the exit
    code its stub selfcheck gives (0 answered, 3 rejected our key, anything else: did not answer)."""
    with tempfile.TemporaryDirectory() as tmp:
        sd, fake_repo, port = Path(tmp) / "ts", Path(tmp) / "repo", free_port()
        stub = str(make_stub(sd, [device("Beta", "beta.x.ts.net", True)], state=state))
        for name, on in (("down", down), ("serve_fails", serve_fails)):
            if on:
                (sd / name).write_text("")
        if foreign:
            (sd / "serve.json").write_text(json.dumps({str(port): foreign}))
        (fake_repo / "postbox").mkdir(parents=True)
        (fake_repo / "postbox" / "selfcheck.py").write_text(STUB_SELFCHECK)
        published = []
        real_repo, real_publish = install.REPO, install.publish_key
        install.REPO = fake_repo
        install.publish_key = lambda machine: published.append(stub_calls(sd, "")) or True   # records how many Tailscale calls came before it
        try:
            with env_set(STUB_DIR=sd, **{f"STUB_SC_{n.upper()}": c for n, c in (sc or {}).items()}):
                _, code, text = run_fn(lambda: install.finish_link(stub, port, PY, "alpha", local_pub, peers, publish))
        finally:
            install.REPO, install.publish_key = real_repo, real_publish
        try:
            forward = json.loads((sd / "serve.json").read_text()).get(str(port))
        except OSError:
            forward = None
        try:
            tested = (sd / "selfchecks.log").read_text().split()
        except OSError:
            tested = []
        return SimpleNamespace(code=code, text=text, error=text[text.index("ERROR"):] if "ERROR" in text else "", port=port, published=published,
                               forward=forward, tested=tested, serve_calls=stub_calls(sd, "serve --bg"))


def shown(case):
    return f"code={case.code} published={case.published} forward={case.forward} tested={case.tested} text={case.text[-500:]}"


case = link_case(down=True, publish=True)
check("Tailscale not running: exit 2 with one plain error, this Mac's public key printed and published BEFORE it, no traceback",
      case.code == 2 and key_before_error(case.text, local_pub) and "Tailscale" in case.error and "install.py" in case.error and "--link" in case.error
      and case.published == [0] and case.tested == [], shown(case))
case = link_case(state="NeedsLogin")
check("Tailscale signed out (BackendState NeedsLogin): exit 2, the key is still printed first, the error says signed out / not running, no forward attempted",
      case.code == 2 and key_before_error(case.text, local_pub) and "signed out / not running" in case.error and "NeedsLogin" in case.error
      and case.serve_calls == 0 and case.tested == [], shown(case))
other_forward = f"127.0.0.1:{free_port()}"
case = link_case(foreign=other_forward)
check("a foreign forward on the port: refused with one plain error, the key is still printed, the other forward is untouched",
      case.code == 2 and key_before_error(case.text, local_pub) and "already forwards" in case.error and case.forward == other_forward
      and case.serve_calls == 0 and case.tested == [], shown(case))
case = link_case(serve_fails=True)
check("Tailscale refuses the new forward: one plain error that shows the command to run by hand, the key is still printed, nobody is tested",
      case.code == 2 and key_before_error(case.text, local_pub) and "did not accept the forward" in case.error and "tailscale serve --bg --tcp" in case.error
      and case.tested == [], shown(case))
case = link_case(publish=True)
check("success: the forward is opened, the linked Mac is tested, the key is shown and published BEFORE Tailscale was touched, exit 0",
      case.code is None and case.forward == f"127.0.0.1:{case.port}" and case.tested == ["beta"] and local_pub in case.text and "ERROR" not in case.text
      and "answered a real test message" in case.text and case.published == [0], shown(case))
case = link_case()
check("without --publish-key nothing is published", case.code is None and case.published == [], shown(case))
case = link_case(sc={"beta": 3})
check("a peer that rejects our key: exit 2 with the FAIL line and the reason, after the key was shown and the forward opened",
      case.code == 2 and "FAIL beta answered but rejected this Mac's key" in case.text and "rejected this Mac's key (see FAIL above)" in case.error
      and "--link --peer alpha" in case.text and local_pub in case.text and case.forward == f"127.0.0.1:{case.port}", shown(case))
case = link_case(sc={"beta": 1})
check("a peer that is asleep or offline is only a warning: exit 0", case.code is None and "WARN beta did not answer" in case.text and "FAIL" not in case.text, shown(case))
case = link_case(sc={"gamma": 3}, peers=dict(ONE_PEER, gamma={"host": "gamma.x.ts.net", "port": lib.DEFAULT_PORT}))
check("every linked Mac is tested, not only the first: one of two rejects the key, so exit 2", case.code == 2 and case.tested == ["beta", "gamma"] and "FAIL gamma answered" in case.text, shown(case))
with tempfile.TemporaryDirectory() as tmp_missing:
    state, text = install.check_peer(Path(tmp_missing) / "no-python", "beta")
check("the peer test cannot even start (its python is missing): reported as 'down' with the reason, not a traceback", state == "down" and "could not run" in text, f"{state} {text}")

# ------------------------------------------------------------------ the installer, linking (sandbox, stub Tailscale, real nats-server -t)
print("installer linking")
if not NATS_BIN:
    print("  skip (nats-server not installed)")
else:
    with tempfile.TemporaryDirectory() as tmp:
        home, repo, sdir = Path(tmp) / "home", Path(tmp) / "postbox", Path(tmp) / "ts"
        home.mkdir()
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".venv", "jetstream", "__pycache__", ".git", "peers"))
        stub = make_stub(sdir, [device("Beta", "beta.x.ts.net", True)])
        port = free_port()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "POSTBOX_STORE_DIR": str(Path(tmp) / "store"),
               "NATS_SERVER_BIN": NATS_BIN, "TAILSCALE_BIN": str(stub), "STUB_DIR": str(sdir),
               "POSTBOX_DOORS": _env.ALL_DOORS_ENV}   # every door on, so the Codex skill checks run
        sup = home / "Library" / "Application Support" / "postbox"

        def run(*args):
            r = subprocess.run([PY, str(repo / "install.py"), "--skip-deps", "--no-launchd", "--port", str(port), *args], env=env, capture_output=True, text=True, timeout=120)
            return r.returncode, r.stdout + r.stderr

        code, out = run("--dry-run", "--link", "--peer", "beta", "--machine", "alpha")
        check("dry run: shows the plan (peer found in Tailscale, forward) and changes nothing",
              code == 0 and "beta at beta.x.ts.net" in out and "Tailscale forward" in out and not sup.exists() and not (repo / "peers").exists(), out[-400:])
        code, out = run("--link", "--peer", "beta", "--machine", "alpha")
        conf_text = (sup / "nats-server.conf").read_text() if (sup / "nats-server.conf").exists() else ""
        link = json.loads((sup / "link.json").read_text()) if (sup / "link.json").exists() else {}
        seed = (sup / "machine.seed").read_text().strip() if (sup / "machine.seed").exists() else "MISSING"
        my_pub = (repo / "peers" / "alpha.pub").read_text().strip() if (repo / "peers" / "alpha.pub").exists() else ""
        check("link: this Mac gets a private key, its public key is saved for the repo, and the link list names the peer",
              code == 0 and stat.S_IMODE((sup / "machine.seed").stat().st_mode) == 0o600 and bool(install.PUBLIC_KEY_RE.match(my_pub))
              and link == {"machine": "alpha", "peers": {"beta": {"host": "beta.x.ts.net", "port": lib.DEFAULT_PORT}}}, out[-500:])
        check("the config demands the login, lists this Mac's key, and holds no private key", f"nkey: {my_pub}" in conf_text and seed not in conf_text and seed not in out)
        check("the config file is private (mode 600)", stat.S_IMODE((sup / "nats-server.conf").stat().st_mode) == 0o600)
        check("it warns that the peer's key is missing instead of trusting anything", "no public key for beta" in out and "# beta" not in conf_text)
        beta_pub = subprocess.run([PY, KEYTOOL, "ensure", str(Path(tmp) / "beta.seed")], capture_output=True, text=True).stdout.strip()
        code, out = run("--peer-key", f"beta={beta_pub}")
        conf_text = (sup / "nats-server.conf").read_text()
        beta_line = [ln for ln in conf_text.splitlines() if beta_pub in ln]
        check("giving the peer's PUBLIC key authorizes it, with the limited permissions only", code == 0 and len(beta_line) == 1 and '"post.>"' in beta_line[0] and "# beta" in beta_line[0], out[-300:])
        code, out = run()
        check("a plain re-run (repair or update) keeps the linked peer instead of dropping it", code == 0 and beta_pub in (sup / "nats-server.conf").read_text())
        skill = home / ".codex" / "skills" / "post" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("an older, locally edited skill\n")
        code, out = run()
        check("a changed Codex skill is left alone by a normal install (a person may have edited it)", code == 0 and skill.read_text() == "an older, locally edited skill\n" and "left as it is" in out)
        code, out = run("--update-codex-skill")
        check("--update-codex-skill replaces it with this repo's copy", code == 0 and skill.read_bytes() == (repo / "codex-skill" / "post" / "SKILL.md").read_bytes() and "replacing the Codex skill" in out, out[-300:])
        code, out = run("--peer-key", "beta=UNOTAKEY")
        check("a mistyped public key is refused before anything is written", code == 2 and "not a public key" in out and beta_pub in (sup / "nats-server.conf").read_text())

        # each peer key is checked on its own BEFORE it is saved: nats-server rejects the WHOLE config over one bad key, and then every
        # later install run would fail the same way without saying which file is wrong
        conf_before = (sup / "nats-server.conf").read_bytes()
        beta_file = repo / "peers" / "beta.pub"
        typo = beta_pub[:10] + ("A" if beta_pub[10] != "A" else "B") + beta_pub[11:]
        check("the typo'd key has the right SHAPE, so only nats-server itself can tell it is wrong", bool(install.PUBLIC_KEY_RE.match(typo)) and typo != beta_pub)
        code, out = run("--peer-key", f"beta={typo}")
        check("a peer key with ONE wrong character is refused before it is saved: the reason is shown, nothing is written, no traceback",
              code == 2 and "Not a valid public nkey" in out and "--peer-key beta=" in out and "Traceback" not in out
              and beta_file.read_text().strip() == beta_pub and (sup / "nats-server.conf").read_bytes() == conf_before, out[-500:])
        code, out = run("--peer-key", f"beta={my_pub}")
        check("this Mac's own public key pasted as the peer's is refused before it is saved, in plain words",
              code == 2 and "OWN public key" in out and "Traceback" not in out and beta_file.read_text().strip() == beta_pub
              and (sup / "nats-server.conf").read_bytes() == conf_before, out[-500:])
        beta_file.write_text(typo + "\n")     # as if a bad file had arrived with a git pull
        code, out = run()
        check("an EXISTING peers/beta.pub with a bad key stops the install and names that exact file and how to fix it",
              code == 2 and str(beta_file) in out and "--peer-key beta=" in out and "Not a valid public nkey" in out and "Traceback" not in out
              and (sup / "nats-server.conf").read_bytes() == conf_before, out[-600:])
        code, out = run("--peer-key", f"beta={beta_pub}")
        check("...and the fix it names works: the right key given with --peer-key repairs it in one run",
              code == 0 and beta_file.read_text().strip() == beta_pub and (sup / "nats-server.conf").read_bytes() == conf_before, out[-400:])
        beta_file.write_text(my_pub + "\n")
        code, out = run()
        check("an existing peers/beta.pub that holds THIS Mac's key is refused and named", code == 2 and str(beta_file) in out and "OWN public key" in out and "Traceback" not in out, out[-400:])
        beta_file.write_text("")
        code, out = run()
        check("an existing EMPTY peers/beta.pub is named too: it is not a key", code == 2 and str(beta_file) in out and "not a public key" in out and "Traceback" not in out, out[-400:])
        code, out = run("--dry-run")
        check("the dry-run plan says a junk peers/beta.pub HOLDS NO VALID KEY instead of calling it merely missing", code == 0 and "HOLDS NO VALID KEY" in out and "MISSING" not in out, out[-400:])
        beta_file.write_text(beta_pub + "\n")
        gamma_file = repo / "peers" / "gamma.pub"
        code, out = run("--link", "--peer", "gamma=gamma.x.ts.net", "--peer-key", f"gamma={beta_pub}")
        check("the same key given for a second Mac is refused before it is saved (two Macs never share a key)",
              code == 2 and "same key as beta" in out and not gamma_file.exists() and "Traceback" not in out, out[-400:])
        gamma_file.write_text(beta_pub + "\n")
        code, out = run("--link", "--peer", "gamma=gamma.x.ts.net")
        check("two peer key files holding the same key: it stops, names both files, and there is no traceback",
              code == 2 and "beta.pub" in out and "gamma.pub" in out and "Traceback" not in out, out[-400:])
        gamma_file.unlink()
        check("...and the mailbox config and the link list were left exactly as they were",
              (sup / "nats-server.conf").read_bytes() == conf_before and set(json.loads((sup / "link.json").read_text())["peers"]) == {"beta"})
        code, out = run("--link", "--peer", "alpha", "--machine", "alpha")
        check("a Mac cannot be linked to itself", code == 2 and "this Mac itself" in out)
        env["TAILSCALE_BIN"] = str(Path(tmp) / "no-such-tailscale")
        code, out = run("--link", "--peer", "beta")
        check("no Tailscale: a plain error naming what to install, nothing half-done", code == 2 and "needs Tailscale" in out)
        env["TAILSCALE_BIN"] = str(stub)
        (sdir / "serve.json").write_text(json.dumps({str(port): f"127.0.0.1:{port}"}))
        subprocess.run([PY, str(repo / "install.py"), "--uninstall", "--port", str(port), "--no-launchd"], env=env, capture_output=True, text=True, timeout=60)
        check("uninstall removes the Tailscale forward it made", not json.loads((sdir / "serve.json").read_text()))
        (sdir / "serve.json").write_text(json.dumps({str(port): f"127.0.0.1:{port}"}))
        (sdir / "off_fails").write_text("")
        r = subprocess.run([PY, str(repo / "install.py"), "--uninstall", "--port", str(port), "--no-launchd"], env=env, capture_output=True, text=True, timeout=60)
        said = r.stdout + r.stderr
        check("uninstall when Tailscale will not remove the forward: says so with the exact command, exits non-zero, and never says 'removed'",
              r.returncode != 0 and "WARN" in said and f"tailscale serve --tcp={port} off" in said and "removed the Tailscale forward" not in said
              and json.loads((sdir / "serve.json").read_text()) == {str(port): f"127.0.0.1:{port}"}, f"{r.returncode} {said[-400:]}")
        (sdir / "off_fails").unlink()
        r = subprocess.run([PY, str(repo / "install.py"), "--uninstall", "--port", str(port), "--no-launchd"], env=env, capture_output=True, text=True, timeout=60)
        check("uninstall once Tailscale cooperates: the forward is removed and the exit code is 0",
              r.returncode == 0 and "removed the Tailscale forward" in r.stdout and not json.loads((sdir / "serve.json").read_text()), f"{r.returncode} {r.stdout} {r.stderr}")

# ------------------------------------------------------------------ main(): the link flow AFTER the services are loaded
print("installer, link flow after the services are loaded (launchd and the post-install verification stubbed out)")
if not NATS_BIN:
    print("  skip (nats-server not installed)")
else:
    def main_case(state="Running", down=False, sc=0, peer="beta=beta.x.ts.net", retry=False):
        """install.main(--link ...) IN THIS PROCESS, in a throwaway HOME and copy of the repo, with launchd and verify() replaced, so the part
        that runs after the services are loaded (public key, Tailscale forward, peer tests, exit code) really runs. -> what happened."""
        with tempfile.TemporaryDirectory() as tmp:
            home, repo, sd, port = Path(tmp) / "home", Path(tmp) / "postbox", Path(tmp) / "ts", free_port()
            home.mkdir()
            shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".venv", "jetstream", "__pycache__", ".git", "peers"))
            (repo / "postbox" / "selfcheck.py").write_text(STUB_SELFCHECK)
            (repo / ".venv" / "bin").mkdir(parents=True)
            os.symlink(PY, repo / ".venv" / "bin" / "python")
            stub = str(make_stub(sd, [device("Beta", "beta.x.ts.net", True)], state=state))
            if down:
                (sd / "down").write_text("")
            real = (install.REPO, install.load_service, install.verify)
            install.REPO, install.load_service, install.verify = repo, (lambda label, plist: None), (lambda *a, **k: True)
            try:
                with env_set(HOME=home, NATS_SERVER_BIN=NATS_BIN, TAILSCALE_BIN=stub, STUB_DIR=sd, STUB_SC_BETA=sc, POSTBOX_STORE_DIR=Path(tmp) / "store"):
                    value, code, text = run_fn(install.main, ["--skip-deps", "--port", str(port), "--link", "--peer", peer, "--machine", "alpha"])
                    again = None
                    if retry:     # the user fixes Tailscale and runs what the error message said: install.py --link
                        (sd / "down").unlink(missing_ok=True)
                        make_stub(sd, [device("Beta", "beta.x.ts.net", True)])
                        (sd / "selfchecks.log").unlink(missing_ok=True)
                        again = run_fn(install.main, ["--skip-deps", "--port", str(port), "--link"])
            finally:
                install.REPO, install.load_service, install.verify = real
            pub_file = repo / "peers" / "alpha.pub"
            try:
                forward = json.loads((sd / "serve.json").read_text()).get(str(port))
            except OSError:
                forward = None
            try:
                tested = (sd / "selfchecks.log").read_text().split()
            except OSError:
                tested = []
            return SimpleNamespace(value=value, code=code, text=text, error=text[text.index("ERROR"):] if "ERROR" in text else "", port=port, forward=forward,
                                   tested=tested, pub=pub_file.read_text().strip() if pub_file.exists() else "", again=again,
                                   wrote=(home / "Library" / "Application Support" / "postbox").exists())

    case = main_case(down=True)
    check("main(): Tailscale off after the services are loaded: exit 2, this Mac's public key printed before the one plain error, no traceback",
          case.code == 2 and key_before_error(case.text, case.pub) and "Tailscale" in case.error, f"{case.code} {case.text[-600:]}")
    case = main_case(state="NeedsLogin")
    check("main(): Tailscale signed out: exit 2 with the plain 'signed out / not running' sentence and the key printed first",
          case.code == 2 and key_before_error(case.text, case.pub) and "signed out / not running" in case.error, f"{case.code} {case.text[-600:]}")
    case = main_case(down=True, peer="beta")
    check("main(): Tailscale off while looking up the linked Mac: one plain error, and nothing was written",
          case.code == 2 and "Tailscale" in case.error and "Nothing was changed" in case.error and not case.wrote and "Traceback" not in case.text, f"{case.code} {case.text[-400:]}")
    case = main_case(sc=3)
    check("main(): a peer that rejects our key: exit 2 with the FAIL line, and the key was printed before it",
          case.code == 2 and "FAIL beta answered but rejected this Mac's key" in case.text and "see FAIL above" in case.error
          and bool(install.PUBLIC_KEY_RE.match(case.pub)) and case.pub in case.text, f"{case.code} {case.text[-600:]}")
    case = main_case()
    check("main(): the whole link flow to the end: forward opened, peer tested, key shown, next steps printed, exit 0",
          case.code is None and case.value == 0 and case.forward == f"127.0.0.1:{case.port}" and case.tested == ["beta"]
          and "PUBLIC KEY" in case.text and bool(install.PUBLIC_KEY_RE.match(case.pub)) and case.pub in case.text and "Installed." in case.text, f"{case.code} {case.text[-600:]}")
    for what, kwargs in (("off", dict(down=True)), ("signed out", dict(state="NeedsLogin"))):
        case = main_case(retry=True, **kwargs)
        again_value, again_code, again_text = case.again
        check(f"main(): Tailscale {what}, then fixed: running `install.py --link` again, as the error says, finishes the link",
              case.code == 2 and again_code is None and again_value == 0 and case.forward == f"127.0.0.1:{case.port}" and case.tested == ["beta"]
              and "Installed." in again_text, f"{case.code} {again_code} {again_text[-500:]}")

# ------------------------------------------------------------------ publishing this Mac's public key (git)
print("publishing the public key")
with tempfile.TemporaryDirectory() as tmp:
    bare, work = Path(tmp) / "remote.git", Path(tmp) / "work"
    git_env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    for cmd, where in ((["git", "init", "-q", "--bare", str(bare)], tmp), (["git", "clone", "-q", str(bare), str(work)], tmp)):
        subprocess.run(cmd, cwd=where, env=git_env, capture_output=True, check=True)
    (work / "peers").mkdir()
    (work / "peers" / "beta.pub").write_text("UPUBLICKEYONLY\n")
    real_repo, real_env = install.REPO, dict(os.environ)
    install.REPO = work
    os.environ.update(git_env)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            done = install.publish_key("beta")
        pushed = subprocess.run(["git", "-C", str(bare), "show", "HEAD:peers/beta.pub"], capture_output=True, text=True).stdout.strip()
        check("publish_key commits only the public key file and pushes it", done is True and pushed == "UPUBLICKEYONLY", f"done={done} pushed={pushed!r}")
        subprocess.run(["git", "-C", str(work), "remote", "set-url", "origin", str(Path(tmp) / "no-such-remote.git")], check=True, capture_output=True)
        (work / "peers" / "beta.pub").write_text("UCHANGEDKEY\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            t0 = time.time()
            done = install.publish_key("beta")
        check("a push that cannot work is a warning with the next step, never a crash, and it does not hang", done is False and "WARN could not publish" in out.getvalue() and time.time() - t0 < 30, out.getvalue()[-200:])
    finally:
        install.REPO = real_repo
        os.environ.clear()
        os.environ.update(real_env)

# ------------------------------------------------------------------ two emulated Macs
print("two emulated Macs")
FAKE = r'''
import os, socket, sys
sockdir, out = sys.argv[2], sys.argv[3]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(os.path.join(sockdir, str(os.getpid()) + ".sock")); s.listen(5)
print("ready", flush=True)
while True:
    c, _ = s.accept()
    data = b""
    while True:
        chunk = c.recv(65536)
        if not chunk:
            break
        data += chunk
    c.close()
    open(out, "ab").write(data)
'''
if not NATS_BIN:
    print("  skip (nats-server not installed)")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)

procs = []
TMP = tempfile.mkdtemp(prefix="pbl", dir="/tmp")   # a short path: a Unix socket path is limited to about 100 characters


def make_mac(name):
    mac = SimpleNamespace(name=name, dir=Path(TMP) / name, port=free_port())
    mac.support, mac.socks = mac.dir / "support", mac.dir / "socks"
    mac.support.mkdir(parents=True)
    mac.socks.mkdir()
    mac.pub = subprocess.run([PY, KEYTOOL, "ensure", str(mac.support / "machine.seed")], capture_output=True, text=True).stdout.strip()
    mac.env = dict(os.environ, POSTBOX_NATS_URL=f"nats://127.0.0.1:{mac.port}", POSTBOX_SUPPORT_DIR=str(mac.support), POSTBOX_CLAUDE_SOCK_DIR=str(mac.socks),
                   POSTBOX_FORWARD_RETRY_S="2")   # mail kept for a sleeping Mac is retried every 2 s here, not every 60 s
    for gone in ("CODEX_THREAD_ID", "POSTBOX_MACHINE"):
        mac.env.pop(gone, None)
    return mac


def link_file(mac, others):
    (mac.support / "link.json").write_text(json.dumps({"machine": mac.name, "peers": {o.name: {"host": "127.0.0.1", "port": o.port} for o in others}}))


def start_relay(mac):
    mac.log = mac.dir / "relay.log"
    mac.relay = subprocess.Popen([PY, RELAY], env=mac.env, stdout=open(mac.log, "w"), stderr=subprocess.STDOUT)
    procs.append(mac.relay)
    assert wait_for(lambda: "relay_ready" in mac.log.read_text()), f"{mac.name}: the relay did not become ready: {mac.log.read_text()[-300:]}"


def start_mailbox(mac, conf_text):
    (mac.dir / "n.conf").write_text(conf_text)
    mac.nats = subprocess.Popen([NATS_BIN, "-c", str(mac.dir / "n.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(mac.nats)
    assert wait_for(lambda: install.port_in_use(mac.port)), f"{mac.name}: the mailbox did not start"


def boot(mac, trusted, others):
    """Start this Mac's mailbox (trusting the keys of `trusted`) and relay; `others` are the Macs it can reach."""
    start_mailbox(mac, install.nats_conf(f"postbox-{mac.name}", mac.port, mac.dir / "store", mac.pub, [(t.name, t.pub) for t in trusted]))
    link_file(mac, others)
    start_relay(mac)


def fake_claude(mac, project):
    folder = mac.dir / project
    folder.mkdir(exist_ok=True)
    out = mac.dir / f"{project}.frames"
    proc = subprocess.Popen([PY, "-c", FAKE, "claude-fake-session", str(mac.socks), str(out)], cwd=folder, stdout=subprocess.PIPE, text=True)
    procs.append(proc)
    proc.stdout.readline()
    proc.sock, proc.out = mac.socks / f"{proc.pid}.sock", out
    return proc


def frames(session):
    return session.out.read_bytes().decode(errors="replace") if session.out.exists() else ""


def post(mac, sender, *args, timeout=60):
    env = dict(mac.env, CLAUDECODE="1", CLAUDE_CODE_MESSAGING_SOCKET=str(sender.sock))
    t0 = time.time()
    r = subprocess.run([PY, POST, *args], env=env, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip(), time.time() - t0


try:
    alpha, beta, gamma = make_mac("alpha"), make_mac("beta"), make_mac("gamma")
    boot(alpha, [beta], [beta])            # alpha trusts beta's key and can reach beta
    boot(beta, [alpha], [alpha])           # beta trusts alpha's key and can reach alpha
    link_file(gamma, [beta])               # gamma can reach beta, but beta has never heard of gamma's key
    a_main, a_other = fake_claude(alpha, "proj-a"), fake_claude(alpha, "proj-x")
    b_main, b_other = fake_claude(beta, "proj-b"), fake_claude(beta, "proj-y")
    g_main = fake_claude(gamma, "proj-g")

    code, out, err, took = post(alpha, a_main, "claude@proj-b@beta", "hello beta, this is alpha")
    check("alpha to beta: delivered, and the answer says which Mac it happened on", code == 0 and out.startswith("delivered:") and out.endswith("[on beta]"), f"{code} {out} {err}")
    got = wait_for(lambda: "hello beta, this is alpha" in frames(b_main))
    check("the session on beta really received the text, from the sender's full address", got and "Message from claude@proj-a@alpha (post box)" in frames(b_main), frames(b_main)[-300:])

    reply_to = re.search(r"post (\S+) \\\"your reply\\\"", frames(b_main))
    check("the message tells beta's session exactly how to answer, machine included", bool(reply_to) and reply_to.group(1) == "claude@proj-a@alpha", frames(b_main)[-400:])
    code, out, err, took = post(beta, b_main, reply_to.group(1) if reply_to else "claude@proj-a@alpha", "got it, beta answering")
    check("the reply, sent with the address exactly as printed, arrives on alpha", code == 0 and wait_for(lambda: "beta answering" in frames(a_main)) and "Message from claude@proj-b@beta" in frames(a_main), f"{code} {out} {err}")

    code, out, err, took = post(alpha, a_main, "claude@@beta", "same project name on beta, empty middle")
    check("claude@@beta targets the same project name on the other Mac (nobody there: kept, not lost)", code == 3 and out.startswith("queued:"), f"{code} {out} {err}")

    code, out, err, took = post(alpha, a_main, "who", "beta")
    check("post who beta lists beta's sessions with their full address", code == 0 and "claude@proj-b@beta" in out and "claude@proj-y@beta" in out, f"{code} {out} {err}")
    code, out, err, took = post(alpha, a_main, "who")
    check("post who on alpha lists alpha's sessions and names the linked Mac", "claude@proj-a" in out and "Other Macs linked: beta" in out, out)

    code, out, err, took = post(alpha, a_main, "claude@proj-x@alpha", "explicitly this Mac")
    check("naming my own Mac in the address is the same as leaving it out (delivered locally)", code == 0 and out.startswith("delivered:") and "[on" not in out and wait_for(lambda: "explicitly this Mac" in frames(a_other)), f"{code} {out} {err}")

    code, out, err, took = post(alpha, a_main, "claude@x@mars", "anyone there")
    check("an unknown Mac: a plain sentence listing the linked ones, nothing sent", code == 2 and "no other Mac called 'mars'" in err and "linked: beta" in err, f"{code} {err}")

    code, out, err, took = post(gamma, g_main, "claude@proj-b@beta", "let me in")
    check("a Mac whose key beta does not know is refused, and told to link", code == 2 and "refused" in err and "does not know this Mac" in err and not wait_for(lambda: "let me in" in frames(b_main), 2), f"{code} {err}")
    sc = subprocess.run([PY, SELFCHECK, "--peer", "beta"], env=gamma.env, capture_output=True, text=True, timeout=60)
    check("selfcheck --peer says 'rejected this Mac's login key' with exit 3 for an unknown key", sc.returncode == 3 and "rejected this Mac's login key" in sc.stderr, sc.stderr)
    sc = subprocess.run([PY, SELFCHECK, "--peer", "beta"], env=alpha.env, capture_output=True, text=True, timeout=60)
    check("selfcheck --peer beta passes for a linked Mac (network, key and beta's relay)", sc.returncode == 0 and "mailbox of beta" in sc.stdout, sc.stdout + sc.stderr)
    sc = subprocess.run([PY, SELFCHECK, "--peer=beta"], env=alpha.env, capture_output=True, text=True, timeout=60)
    check("selfcheck --peer=beta (equals form) tests beta, not this Mac's own mailbox", sc.returncode == 0 and "mailbox of beta" in sc.stdout, sc.stdout + sc.stderr)
    for what, argv in (("a bare --peer", ["--peer"]), ("an empty --peer name", ["--peer", ""]), ("an unknown option", ["--bogus"]), ("a stray word", ["beta"])):
        sc = subprocess.run([PY, SELFCHECK, *argv], env=alpha.env, capture_output=True, text=True, timeout=60)
        check(f"selfcheck with {what}: exit 2 with usage, and it does NOT quietly test this Mac's own mailbox instead",
              sc.returncode == 2 and "usage" in sc.stderr.lower() and "selfcheck ok" not in sc.stdout, f"{sc.returncode} {sc.stdout} {sc.stderr}")
    sc = subprocess.run([PY, SELFCHECK], env=alpha.env, capture_output=True, text=True, timeout=60)
    check("selfcheck with no --peer at all tests this Mac's own mailbox", sc.returncode == 0 and "selfcheck ok: the mailbox, its stream" in sc.stdout, sc.stdout + sc.stderr)

    async def limits():
        import nats
        errors = []

        async def on_error(e=None):
            errors.append(repr(e))

        async def sink(m):
            pass

        seed = (alpha.support / "machine.seed").read_text().strip()
        nc = await nats.connect(f"nats://127.0.0.1:{beta.port}", error_cb=on_error, nkeys_seed_str=seed)
        await nc.publish("foo.bar", b"x")
        await nc.flush()
        await asyncio.sleep(0.3)
        no_publish = any("ermission" in e for e in errors)
        errors.clear()
        await nc.subscribe("post.>", cb=sink)
        await nc.flush()
        await asyncio.sleep(0.3)
        no_read = any("ermission" in e for e in errors)
        try:
            await nc.request("$JS.API.STREAM.PURGE.POST", b"", timeout=1.5)
            purged = True
        except Exception:  # noqa: BLE001
            purged = False
        await nc.close()
        local = await lib.connect_fast(lib.Target(f"nats://127.0.0.1:{beta.port}", (beta.support / "machine.seed").read_text().strip(), "beta"))
        held = (await local.jetstream().stream_info("POST")).state.messages
        await local.close()
        return no_publish, no_read, purged, held

    post(alpha, a_main, "claude@nobody@beta", "keep this one in the mailbox")
    no_publish, no_read, purged, held = asyncio.run(limits())
    check("a linked Mac's key cannot publish outside post.* / who.*", no_publish)
    check("a linked Mac's key cannot read other people's mail", no_read)
    check("a linked Mac's key cannot purge or administer the stream (mail kept for nobody-there is still stored)", not purged and held >= 1, f"purged={purged} held={held}")

    async def stored_headers(mac, subject):
        local = await lib.connect_fast(lib.Target(f"nats://127.0.0.1:{mac.port}", (mac.support / "machine.seed").read_text().strip(), mac.name))
        try:
            return (await local.jetstream().get_last_msg("POST", subject)).headers or {}
        finally:
            await local.close()

    remote_headers = asyncio.run(stored_headers(beta, "post.nobody.claude"))
    check("mail from another Mac carries the full sender address and NO process number (a pid means nothing on the receiving Mac)",
          remote_headers.get("From") == "claude@proj-a@alpha" and "From-Pid" not in remote_headers, str(remote_headers))
    post(alpha, a_main, "claude@localnobody", "keep this local one")
    local_headers = asyncio.run(stored_headers(alpha, "post.localnobody.claude"))
    check("mail from a session on the same Mac still carries its process number (a session never gets its own message)",
          local_headers.get("From") == "claude@proj-a" and local_headers.get("From-Pid") == str(a_main.pid), str(local_headers))

    async def no_login():
        try:
            await lib.connect_fast(lib.Target(f"nats://127.0.0.1:{alpha.port}", None, "alpha"))
        except lib.MailboxError as exc:
            return exc.refused
        return None
    check("a process with no key cannot even connect to a mailbox on the same Mac", asyncio.run(no_login()) is True)

    beta.relay.terminate()
    beta.relay.wait(timeout=10)
    sc = subprocess.run([PY, SELFCHECK, "--peer", "beta"], env=alpha.env, capture_output=True, text=True, timeout=60)
    check("beta's relay stopped: selfcheck --peer says the relay never answered (exit 1)", sc.returncode == 1 and "relay never answered" in sc.stderr, sc.stderr)

    beta.nats.terminate()
    beta.nats.wait(timeout=10)
    code, out, err, took = post(alpha, a_main, "claude@proj-b@beta", "are you there")
    check("beta down: alpha does not freeze, exits 3 and says the mail is stored here and forwarded when beta answers",
          code == 3 and took < 12 and out.startswith("queued: beta is not reachable right now; stored here and forwarded when it answers"), f"{code} {took:.1f}s {out} {err}")
    code, out, err, took = post(alpha, a_main, "who", "beta")
    check("post who on a Mac that is down still fails in plain words (nothing is stored for a question)", code == 2 and "cannot reach beta's post box" in err, f"{code} {err}")
    time.sleep(3)   # the stored mail is now older than the 1 second stale limit set for beta below (the real default is 30 minutes)
    beta.env["POSTBOX_STALE_AFTER_S"] = "1"
    start_mailbox(beta, install.nats_conf("postbox-beta", beta.port, beta.dir / "store", beta.pub, [(alpha.name, alpha.pub)]))
    start_relay(beta)
    got = wait_for(lambda: "are you there" in frames(b_main), 30)
    check("beta wakes up: the mail alpha stored while beta was down arrives by itself, from alpha's full address", got and "Message from claude@proj-a@alpha (post box)" in frames(b_main), frames(b_main)[-300:])
    check("and it carries the stale banner, because it waited longer than the limit", "STALE MAIL: this message waited" in frames(b_main) and "Do NOT act on it yet" in frames(b_main), frames(b_main)[-600:])
    check("alpha's relay logged the forward", wait_for(lambda: '"event": "forwarded"' in alpha.log.read_text(), 10), alpha.log.read_text()[-300:])
    body = "(post box):\\nare you there"   # the message body itself (the stale banner quotes it a second time inside the question it asks)
    forwarded_once = frames(b_main).count(body)
    time.sleep(4)
    check("it arrived exactly once (alpha's copy was acknowledged, not sent again)", forwarded_once == 1 and frames(b_main).count(body) == 1, f"{forwarded_once} then {frames(b_main).count(body)}")

    # a Mac from before logins existed: no key, unauthenticated mailbox. Pulling the new code must not break it.
    old = make_mac("old")
    (old.support / "machine.seed").unlink()
    start_mailbox(old, install.nats_conf("postbox-old", old.port, old.dir / "store"))
    start_relay(old)
    o_main = fake_claude(old, "proj-o")
    code, out, err, took = post(old, o_main, "claude@nowhere", "old install, no login")
    check("an install from before logins keeps working after the new code is pulled", code == 3 and out.startswith("queued:"), f"{code} {out} {err}")
finally:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
