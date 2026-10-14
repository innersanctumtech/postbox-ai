#!/usr/bin/env python3
"""Install the post box on this Mac (macOS only). Safe to run again: it repairs and re-verifies.

    python3 install.py                 install (or repair) and verify
    python3 install.py --dry-run       show what would happen, change nothing
    python3 install.py --check         only verify an existing install
    python3 install.py --uninstall     stop and remove the services and the `post` link (code and data stay)
    python3 install.py --link --peer mac-b        also link this Mac to another Mac (see "Linking Macs" below)

Options: --port N (default 4222), --label-prefix P (default com.postbox), --skip-deps, --no-launchd, --force
         --link, --peer NAME[=HOST[:PORT]] (repeatable), --peer-key NAME=KEY, --publish-key, --machine NAME,
         --update-claude-skill, --update-codex-skill (only with the codex door on)
Environment: NATS_SERVER_BIN (path of nats-server), TAILSCALE_BIN, POSTBOX_STORE_DIR (default <repo>/jetstream),
             POSTBOX_SUPPORT_DIR (default ~/Library/Application Support/postbox)

Every install gives this Mac a login key (an nkey pair: the private half stays in machine.seed, mode 600, and never leaves) and
the mailbox demands that key from every client, so nothing else on the Mac can put text in front of an agent. Without the key library
(nkeys) the config is written without a login ONLY for a bare --skip-deps test setup (no key file yet, no linked Macs); in every other case
the install stops and changes nothing, because an open mailbox behind Tailscale's forward would be reachable from the whole tailnet.

Linking Macs: --link makes this Mac reachable from another Mac and lets it reach that one. Nothing secret is exchanged, only
PUBLIC keys: this Mac's goes to peers/<machine>.pub in this folder (give it to the other Mac by hand, see --peer-key), and the other Mac's is read from
peers/<name>.pub (or given with --peer-key NAME=KEY). Only Macs named with --peer are ever trusted. Reachability is Tailscale's
private forward (`tailscale serve --tcp`, tailnet only, never the internet); the mailbox itself still listens on 127.0.0.1 only.
The peer's tailnet name is looked up in Tailscale at run time unless given as --peer NAME=HOST. Every peer key is checked on its own
(nats-server -t) before it is saved or used, and a bad peers/<name>.pub is named in the error. A Tailscale that is signed out or stopped ends
the install with one plain error AFTER this Mac's public key has been printed. --uninstall exits non-zero when the Tailscale forward is
still in place afterwards.

Everything machine-specific (home folder, user, Homebrew location, this folder) is worked out here at run time and
written into per-machine files (launchd plists, NATS config). Nothing in the repo is tied to one Mac. It needs
Homebrew (for nats-server) and Python 3.9+. It never touches another post box's services: if one is already
installed or listening on the port it stops and says so (use --force only if you know why).

Verification is a real test, not a look at the process list: NATS answers, the relay is running and logging, and a test
message makes the whole round trip (postbox/selfcheck.py). Re-running it after a Homebrew upgrade repairs a broken
python environment (and, when the codex door is on, re-reads where node and codex live).
"""
import argparse
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "postbox"))
import lib  # noqa: E402  (standard library only; shared with the running post box so both agree what a machine name is)

DEFAULT_PORT = lib.DEFAULT_PORT
MIN_PYTHON = (3, 9)
PUBLIC_KEY_RE = re.compile(r"^U[A-Z2-7]{55}$")


def say(message):
    print(f"postbox-install: {message}", flush=True)


def die(message, code=2):
    print(f"postbox-install: ERROR: {message}", file=sys.stderr, flush=True)
    sys.exit(code)


def home():
    return Path(os.environ["HOME"])


def agents_dir():
    return home() / "Library" / "LaunchAgents"


def logs_dir():
    return home() / "Library" / "Logs"


def support_dir():
    return lib.support_dir()


def local_bin():
    return home() / ".local" / "bin"


def store_dir():
    return Path(os.environ.get("POSTBOX_STORE_DIR") or REPO / "jetstream")


def labels(prefix):
    return f"{prefix}.nats", f"{prefix}.relay"


# ------------------------------------------------------------------ what gets written (pure functions, unit-tested)
def quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def peer_permissions(key):
    """What a linked Mac's key may do: drop mail into post.<project>.<agent>, ask who.sessions, and hear replies on ITS OWN inbox name.
    It must never be allowed to listen on _INBOX.> : the relay's mail deliveries and every other reply travel on _INBOX names, so a wider
    grant lets the other Mac read this Mac's mail."""
    return f'permissions: {{ publish: {{ allow: ["post.>", "who.>"] }}, subscribe: {{ allow: ["{lib.peer_inbox_prefix(key)}.>"] }} }}'


def nats_conf(server_name, port, store, local_key=None, peers=()):
    """The mailbox's NATS config. `local_key` is this Mac's public key; `peers` is [(name, public key)] of the other Macs allowed in.

    With a local key EVERY client must log in (processes on this Mac too). A peer's key may only drop mail into
    post.<project>.<agent>, ask who.sessions and hear its own replies: it cannot read anyone's mail, purge or administer the stream.
    Names and keys are checked here, because a bad line in this file stops the mailbox from starting."""
    if local_key is not None and not PUBLIC_KEY_RE.match(local_key):
        raise ValueError("this Mac's public key is malformed")
    head = "# Agent post box: NATS server with JetStream (durable mailboxes). Written by install.py; run it again instead of editing.\n"
    if local_key is None:
        head += ("# NO LOGIN (test setups only) and 127.0.0.1 only. A real install always has a login: anyone who can publish here\n"
                 "# can put text in front of an agent.\n")
    else:
        head += ("# EVERY client must log in with a key, processes on this Mac included. Listens on 127.0.0.1 only: another Mac comes in\n"
                 "# through Tailscale's private forward (install.py --link), never by binding another address.\n")
    conf = (
        head +
        f"server_name: {server_name}\n"
        f"listen: 127.0.0.1:{port}\n"
        "jetstream {\n"
        f"  store_dir: {quote(store)}\n"
        "  max_memory_store: 256MB\n"
        "  max_file_store: 2GB\n"
        "}\n"
    )
    if local_key is None:
        return conf
    users = f"    {{ nkey: {local_key} }}    # this Mac\n"
    seen = {local_key}
    for name, key in peers:
        if name != lib.norm(name) or not PUBLIC_KEY_RE.match(key):
            raise ValueError(f"peer {name!r} has a malformed name or key")
        if key in seen:
            raise ValueError(f"the public key for peer {name!r} is this Mac's own key or another peer's (nats-server refuses duplicate keys)")
        seen.add(key)
        users += f"    {{ nkey: {key}, {peer_permissions(key)} }}    # {name}\n"
    return conf + "authorization {\n  users = [\n" + users + "  ]\n}\n"


def nats_plist(label, nats_bin, conf, repo, log):
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(nats_bin), "-c", str(conf)],
        "WorkingDirectory": str(repo),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    })


def relay_plist(label, python, relay_py, repo, path_env, nats_url, log):
    env = {"PATH": path_env}
    if nats_url:
        env["POSTBOX_NATS_URL"] = nats_url
    return plistlib.dumps({
        "Label": label,
        "ProgramArguments": [str(python), str(relay_py)],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": env,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    })


def relay_path():
    """PATH for the relay: launchd starts with almost nothing. With the codex door on it must also find `codex` (and the `node` behind it)."""
    dirs = []
    for tool in ("brew", "nats-server") + (("codex", "node") if lib.doors.enabled("codex") else ()):
        found = shutil.which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", str(local_bin()), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen, out = set(), []
    for d in dirs:
        if d not in seen and (os.path.isdir(d) or d == str(local_bin())):
            seen.add(d)
            out.append(d)
    return ":".join(out)


# ------------------------------------------------------------------ probes
def find_nats():
    forced = os.environ.get("NATS_SERVER_BIN")
    if forced:
        return forced if os.access(forced, os.X_OK) else None
    found = shutil.which("nats-server")
    if found:
        return found
    for prefix in ("/opt/homebrew", "/usr/local"):
        candidate = f"{prefix}/opt/nats-server/bin/nats-server"
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def find_brew():
    """Homebrew, even in a shell that never ran `brew shellenv` (a fresh Apple Silicon Mac has it off PATH)."""
    found = shutil.which("brew")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def venv_healthy(python):
    """The environment can run and has the mailbox library (a Homebrew Python that was upgraded or removed breaks it)."""
    try:
        return subprocess.run([str(python), "-c", "import nats"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def port_in_use(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def own_plists(prefix):
    return [agents_dir() / f"{name}.plist" for name in labels(prefix)]


def other_postbox_plists(prefix):
    mine = {p.name for p in own_plists(prefix)}
    return sorted(p for p in agents_dir().glob("*postbox*.plist") if p.name not in mine)


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def domain():
    return f"gui/{os.getuid()}"


def service_loaded(label):
    return launchctl("print", f"{domain()}/{label}").returncode == 0


def load_service(label, plist):
    """(Re)load one launchd service. `bootout` returns before the old process has gone, and a `bootstrap` that comes too
    early fails with a confusing "Input/output error", so wait until the old one is really gone. A label that was
    disabled earlier fails the same way, so enable it first. Every failure shows launchctl's own words."""
    stop_wait = float(os.environ.get("POSTBOX_LAUNCHD_STOP_WAIT", "15"))
    retry_wait = float(os.environ.get("POSTBOX_LAUNCHD_RETRY_WAIT", "2"))
    if service_loaded(label):
        launchctl("bootout", domain(), str(plist))
        end = time.time() + stop_wait
        while time.time() < end and service_loaded(label):
            time.sleep(0.3)
        if service_loaded(label):
            die(f"the old {label} did not stop within {stop_wait:g} s. Stop it by hand (launchctl bootout {domain()}/{label}) and run this again.")
    launchctl("enable", f"{domain()}/{label}")
    problem = ""
    for _ in range(3):
        r = launchctl("bootstrap", domain(), str(plist))
        if r.returncode == 0:
            launchctl("kickstart", "-k", f"{domain()}/{label}")
            return
        problem = (r.stderr or r.stdout).strip()
        time.sleep(retry_wait)
    hint = "You are connected over SSH: run this from a normal Terminal window on the Mac. " if os.environ.get("SSH_CONNECTION") else ""
    die(f"launchctl could not load {plist.name} after 3 tries: {problem}. {hint}Load it by hand to see more: launchctl bootstrap {domain()} {plist}")


def relay_alive(since, max_age=900):
    """The relay log has a recent life sign (it writes a heartbeat every 5 minutes when quiet, and a line per delivery).
    Right after an install (`since` > 0) it must also contain a relay_ready NEWER than the install began."""
    log = logs_dir() / "postbox-relay.log"
    if not log.is_file():
        return False
    import json
    newest, ready = 0.0, False
    for line in log.read_text(errors="replace").splitlines()[-200:]:
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            stamp = datetime.strptime(row["t"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
            event = row.get("event")
        except (ValueError, KeyError, TypeError):
            continue
        if event in ("relay_error", "handle_error"):
            continue
        newest = max(newest, stamp)
        if event == "relay_ready" and stamp >= since - 2:
            ready = True
    return (time.time() - newest < max_age) and (ready if since else True)


def selfcheck(port, python):
    env = dict(os.environ)
    if port != DEFAULT_PORT:
        env["POSTBOX_NATS_URL"] = f"nats://127.0.0.1:{port}"
    r = subprocess.run([str(python), str(REPO / "postbox" / "selfcheck.py")], capture_output=True, text=True, env=env, timeout=60)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def relay_can_run_codex():
    """True/False: does `codex --version` work with the relay's PATH? None: Codex is not installed on this Mac."""
    path_env = relay_path()
    if shutil.which("codex") is None:
        return None
    codex = shutil.which("codex", path=path_env)
    if codex is None:
        return False
    try:
        return subprocess.run([codex, "--version"], capture_output=True, env=dict(os.environ, PATH=path_env), timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def verify(port, since, python, wait=40):
    """Print one line per check; True only if every real check passes."""
    ok = True
    end = time.time() + wait
    while time.time() < end and not port_in_use(port):
        time.sleep(1)
    up = port_in_use(port)
    say(f"  {'ok  ' if up else 'FAIL'} NATS answers on 127.0.0.1:{port}")
    ok &= up
    if python is None:
        say("  skip relay and round-trip checks (no python environment was built)")
        return ok
    while time.time() < end and not relay_alive(since):
        time.sleep(1)
    alive = relay_alive(since)
    say(f"  {'ok  ' if alive else 'FAIL'} relay is running and logging ({logs_dir() / 'postbox-relay.log'})")
    ok &= alive
    good, why = False, ""
    for _ in range(4):                       # the relay may need a moment after it logs relay_ready
        good, why = selfcheck(port, python)
        if good:
            break
        time.sleep(2)
    say(f"  {'ok  ' if good else 'FAIL'} a real test message made the round trip (post to mailbox to relay and back)" + ("" if good else f": {why}"))
    ok &= good
    codex = relay_can_run_codex() if lib.doors.enabled("codex") else None
    if codex is False:
        say("  WARN the relay cannot run `codex` with the PATH it was installed with (Codex sessions will not receive mail). "
            "Run this installer again after Homebrew changed node or codex.")
    elif codex:
        say("  ok   the relay can run `codex`")
    return ok


# ------------------------------------------------------------------ login keys, linked Macs, Tailscale
def write_private(path, text):
    """Write a file only its owner can read (config and keys), replacing any old one."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def key_python(venv_python):
    """A python that can import the key library: the post box's own environment, else the one running this script."""
    for candidate in (venv_python, Path(sys.executable)):
        if candidate and Path(candidate).exists():
            try:
                if subprocess.run([str(candidate), "-c", "import nkeys"], capture_output=True, timeout=30).returncode == 0:
                    return Path(candidate)
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def keytool(python, *args):
    r = subprocess.run([str(python), str(REPO / "postbox" / "keytool.py"), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        die("the key tool failed: " + (r.stderr or r.stdout).strip()[:300])
    return r.stdout.strip()


def login_python(venv_python, skip_deps, seed_file, peers):
    """The python that can make and read this Mac's login key (it needs the nkeys library), or None when the mailbox config may be written
    WITHOUT a login. That is allowed for a bare test setup only: --skip-deps, no key file yet and no linked Macs. Every other case without a
    key-capable python stops here, before anything is written: a no-login config next to an existing key, or next to a Tailscale forward for
    linked Macs, would leave the mailbox open to every device on the tailnet."""
    found = key_python(venv_python)
    if found is not None:
        return found
    if not skip_deps:
        die("cannot make this Mac's login key: no python environment with the key library (nkeys). Run this again without --skip-deps.")
    reasons = []
    if seed_file.exists():
        reasons.append(f"this Mac already has a login key ({seed_file})")
    if peers:
        reasons.append(f"this Mac is linked to other Macs ({', '.join(sorted(peers))})")
    if reasons:
        die(f"{' and '.join(reasons)}, but no python here can load the key library (nkeys). Writing the mailbox config without a login would leave it "
            f"open to every device on your tailnet, so nothing was changed. Run the installer with the post box's own python ({venv_python} install.py ...) "
            "or without --skip-deps.")
    return None


def peer_key_file(name):
    return REPO / "peers" / f"{name}.pub"


def read_peer_key(name):
    """Another Mac's PUBLIC key from peers/<name>.pub, or None when the file is missing or not a key."""
    try:
        key = peer_key_file(name).read_text().strip()
    except OSError:
        return None
    return key if PUBLIC_KEY_RE.match(key) else None


def peer_key_label(name):
    """For the plan: where this linked Mac's public key is, or what is wrong with it."""
    if read_peer_key(name):
        return f"peers/{name}.pub"
    if peer_key_file(name).exists():
        return f"peers/{name}.pub HOLDS NO VALID KEY: the install stops until that is fixed"
    return "MISSING, it cannot send to this Mac yet"


def save_public_key(name, key):
    if not PUBLIC_KEY_RE.match(key):
        die(f"the key given for {name} is not a public key (it should be U followed by 55 letters and digits)")
    peer_key_file(name).parent.mkdir(exist_ok=True)
    peer_key_file(name).write_text(key + "\n")


def parse_peer_spec(spec):
    """'mac-b' -> ('mac-b', None, None); 'mac-b=host.example:4222' -> ('mac-b', 'host.example', 4222)."""
    name, _, where = spec.partition("=")
    host, _, port = where.partition(":")
    name = lib.norm(name)
    if name == "unknown":
        die(f"bad --peer {spec!r}: use --peer NAME or --peer NAME=HOST")
    try:
        return name, (host or None), (int(port) if port else None)
    except ValueError:
        die(f"bad --peer {spec!r}: the port must be a number")


def find_tailscale():
    forced = os.environ.get("TAILSCALE_BIN")
    if forced:
        return forced if os.access(forced, os.X_OK) else None
    found = shutil.which("tailscale")
    if found:
        return found
    app = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    return app if os.access(app, os.X_OK) else None


class TailscaleError(RuntimeError):
    """Tailscale cannot be used right now (not running, signed out, not answering). The message is a complete plain sentence that ends with
    the next step, so a caller can show it as it is."""


TAILSCALE_APP = "Open the Tailscale app on this Mac and make sure it is running and signed in."
TAILSCALE_STATE_STEP = {
    "NeedsLogin": "Open the Tailscale app and sign in.",
    "Stopped": "Open the Tailscale app and turn it on.",
    "NeedsMachineAuth": "An administrator has to approve this Mac in the Tailscale admin console.",
}


def tailscale_json(ts, *args):
    """Run `tailscale <args>` and return its JSON answer (always a dict). Any trouble is a TailscaleError in plain words, never a traceback."""
    cmd = "tailscale " + " ".join(args)
    try:
        r = subprocess.run([ts, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TailscaleError(f"`{cmd}` could not run ({type(exc).__name__}: {exc}). {TAILSCALE_APP}")
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip()[:300] or "no reason given"
        raise TailscaleError(f"Tailscale is not answering on this Mac (`{cmd}` failed: {detail}). {TAILSCALE_APP}")
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise TailscaleError(f"`{cmd}` did not answer in the expected JSON form. Update the Tailscale app, then run this again.")
    return data


def tailscale_status(ts):
    """`tailscale status --json`, but only when Tailscale is really up on this Mac. BackendState says so: anything other than Running (signed
    out, stopped, still starting) is one plain sentence here, instead of a confusing failure later such as "no device found"."""
    status = tailscale_json(ts, "status", "--json")
    state = str(status.get("BackendState") or "")
    if state != "Running":
        step = TAILSCALE_STATE_STEP.get(state, "Open the Tailscale app and wait until it says it is connected.")
        raise TailscaleError(f"Tailscale is signed out / not running on this Mac (its state is {state or 'unknown'}, it should be Running). {step}")
    return status


def serve_forward(ts, port):
    """Where Tailscale forwards tailnet TCP port `port` on this Mac (for example '127.0.0.1:<port>'), or None."""
    tcp = tailscale_json(ts, "serve", "status", "--json").get("TCP")
    entry = tcp.get(str(port)) if isinstance(tcp, dict) else None
    return entry.get("TCPForward") if isinstance(entry, dict) else None


def ensure_serve(ts, port):
    """Make Tailscale forward tailnet TCP port `port` to the local mailbox. Tailnet only (serve; funnel would be the internet).
    Raises TailscaleError (one plain sentence with the next step) when Tailscale cannot be used right now. Stops with a plain error, and
    changes nothing, when the port already goes somewhere else."""
    tailscale_status(ts)     # signed out or stopped: say so plainly now, not through whatever the next command happens to fail with
    want = f"127.0.0.1:{port}"
    have = serve_forward(ts, port)
    if have == want:
        say(f"  ok   Tailscale already forwards tailnet port {port} to the mailbox")
        return
    if have:
        die(f"Tailscale already forwards tailnet port {port} to {have}, not to the post box. Nothing was changed; use another --port or remove that forward.")
    by_hand = f"tailscale serve --bg --tcp {port} tcp://{want}"
    try:
        r = subprocess.run([ts, "serve", "--bg", "--tcp", str(port), f"tcp://{want}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TailscaleError(f"`tailscale serve` did not finish ({type(exc).__name__}). Run `{by_hand}` by hand to see why.")
    if serve_forward(ts, port) != want:
        reason = ((r.stderr or r.stdout).strip()[:300] or "no reason given").rstrip(".")
        die(f"Tailscale did not accept the forward: {reason}. Run `{by_hand}` by hand to see more, fix what it says, then run this again.")
    say(f"  ok   Tailscale now forwards tailnet port {port} to the mailbox (private to your tailnet)")


def remove_serve(ts, port):
    """Remove OUR forward only, never someone else's forward on that port. True when none of ours is left. False when it is still there or
    could not be checked: the WARN names the exact command to run by hand, and the caller must not report success."""
    ours = f"127.0.0.1:{port}"
    by_hand = f"tailscale serve --tcp={port} off"
    try:
        if serve_forward(ts, port) != ours:
            return True
        subprocess.run([ts, "serve", f"--tcp={port}", "off"], capture_output=True, text=True, timeout=60)
        still = serve_forward(ts, port)          # read it back: the `off` call's own exit status proves nothing about what Tailscale now holds
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        say(f"WARN could not check or remove the Tailscale forward for port {port} ({exc}). When Tailscale works again, remove it with: {by_hand}")
        return False
    if still == ours:
        say(f"WARN the Tailscale forward for port {port} is STILL in place (`tailscale serve off` did not remove it). Remove it yourself with: {by_hand}")
        return False
    say(f"removed the Tailscale forward for port {port}")
    return True


def resolve_peer_host(ts, name):
    """Tailnet name of the Mac called `name`. Exactly one device must match; an online one wins over an offline one of the same name.
    Raises TailscaleError when Tailscale is signed out or not running. A name that matches nothing, or several devices, stops with a plain error."""
    tailnet = tailscale_status(ts).get("Peer")
    devices = [p for p in tailnet.values() if isinstance(p, dict)] if isinstance(tailnet, dict) else []
    named = [p for p in devices if lib.norm(p.get("HostName", "")) == name and p.get("DNSName")]
    found = [p for p in named if p.get("Online")] if len(named) > 1 else named
    if len(found) == 1:
        return found[0]["DNSName"].rstrip(".")
    again = f"python3 {REPO / 'install.py'} --link --peer {name}"
    if not named:
        others = ", ".join(sorted({str(p["HostName"]) for p in devices if p.get("HostName")})) or "no other devices"
        die(f"no device called '{name}' is on this Mac's tailnet (Tailscale lists: {others}). Check that {name} is switched on and signed in to the same "
            f"Tailscale account, then run `{again}` to look it up again, or name it yourself with `--peer {name}=<its tailnet name>`.")
    options = ", ".join(str(p["DNSName"]).rstrip(".") for p in named)
    die(f"cannot tell which tailnet device is '{name}' (matches: {options}). Say which one with `--peer {name}=<tailnet name>`. "
        f"Once only one of them is online, `{again}` looks it up again by itself.")


def check_peer(python, name):
    """('ok'|'refused'|'down', text). 'refused' is a real fault (the other Mac rejects our key); 'down' is asleep, offline or not linked yet."""
    try:
        r = subprocess.run([str(python), str(REPO / "postbox" / "selfcheck.py"), "--peer", name], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return "down", f"the test itself could not run ({type(exc).__name__}: {exc})"
    return ("ok" if r.returncode == 0 else "refused" if r.returncode == 3 else "down"), (r.stderr or r.stdout).strip()


def verify_link(python, peers):
    """One line per linked Mac. False only when a Mac answered and rejected our key; a Mac that is off or not linked yet is a warning."""
    good = True
    for name in sorted(peers):
        state, text = check_peer(python, name)
        if state == "ok":
            say(f"  ok   {name}'s post box answered a real test message (network, our key and its relay all work)")
        elif state == "refused":
            good = False
            say(f"  FAIL {name} answered but rejected this Mac's key. It needs this Mac's public key: run install.py --link --peer {lib.my_machine()} there after pulling this repo. ({text})")
        else:
            say(f"  WARN {name} did not answer ({text}). Expected if it is asleep or offline, or has not run its own install.py --link yet.")
    return good


def validate_conf(nats_bin, text, what="the new config", fix=""):
    """Ask nats-server itself whether `text` is a valid config BEFORE anything is replaced: a bad line stops the mailbox from starting.
    Returns True when it is valid. Otherwise stops with a plain error that says what was rejected (`what`) and, when given, how to fix it."""
    tmp = support_dir() / "nats-server.conf.check"
    write_private(tmp, text)
    try:
        r = subprocess.run([str(nats_bin), "-t", "-c", str(tmp)], capture_output=True, text=True, timeout=30)
    finally:
        tmp.unlink(missing_ok=True)
    if r.returncode != 0:
        die(f"nats-server rejected {what}, nothing was changed: {(r.stderr or r.stdout).strip()[-300:]}" + (f". {fix}" if fix else ""))
    return True


def peer_key_fix(name, path):
    """How to put a wrong peers/<name>.pub right (part of the error that names the file)."""
    return (f"To fix it, run `python3 {REPO / 'install.py'} --peer-key {name}=<{name}'s public key>` (install.py prints that key on {name} when it links), "
            f"or delete {path} and run git pull.")


def check_peer_key(nats_bin, name, key, local_key, where, fix, taken=None):
    """Refuse a linked Mac's public key that cannot work, BEFORE it is saved or becomes part of the config: nats-server rejects the whole config
    over one bad key, and then every later install run fails the same way. `where` says which key this is (a file or an option), `fix` how to
    put it right, `taken` maps the keys of the other linked Macs to their names. Returns True when the key is fine."""
    if not PUBLIC_KEY_RE.match(key):
        die(f"{where} is not a public key (it should be U followed by 55 capital letters and digits; this one has {len(key)} characters). {fix}")
    if key == local_key:
        die(f"{where} is this Mac's OWN public key, not {name}'s: every Mac has its own. {fix}")
    other = (taken or {}).get(key)
    if other and other != name:
        die(f"{where} is the same key as {other}'s (peers/{other}.pub): every Mac has its own. {fix}")
    try:
        text = nats_conf("postbox-check", DEFAULT_PORT, store_dir(), local_key, [(name, key)])     # a config with ONLY this peer's key in it
    except ValueError as exc:
        die(f"{where} cannot be used: {exc}. {fix}")
    return validate_conf(nats_bin, text, what=where, fix=f"It was probably mistyped or cut short. {fix}")


def trusted_peer_keys(nats_bin, peers, local_key):
    """[(name, public key)] for the linked Macs, read from their peers/<name>.pub files. A missing file is only a warning (that Mac has not shared
    its key yet, so it cannot send here). A file that is there but wrong stops the install and is NAMED, because nats-server would refuse the
    whole config over it and every later run would fail the same way."""
    keys, owner = [], {}
    for name in sorted(peers):
        path = peer_key_file(name)
        if not path.exists():
            say(f"  WARN no public key for {name} yet (peers/{name}.pub): this Mac can send to {name}, but {name} cannot send here until its key is added (--peer-key {name}=<its public key>)")
            continue
        try:
            key = path.read_text().strip()
        except (OSError, UnicodeDecodeError) as exc:
            die(f"cannot read {path} ({type(exc).__name__}). {peer_key_fix(name, path)}")
        check_peer_key(nats_bin, name, key, local_key, f"the key in {path}", peer_key_fix(name, path), taken=owner)
        owner[key] = name
        keys.append((name, key))
    return keys


def publish_key(machine):
    """Best effort: commit and push peers/<machine>.pub so the other Mac can pull it. A failure is reported, never fatal."""
    rel = f"peers/{machine}.pub"
    steps = (["git", "add", rel],
             ["git", "-c", "commit.gpgsign=false", "commit", "-m", f"post box: public key of {machine}", "--", rel],
             ["git", "push"])
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")   # a missing login must fail at once, never sit waiting for a password
    for cmd in steps:
        try:
            r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=90, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            say(f"  WARN could not publish {rel} ({' '.join(cmd[:3])}: {exc}). Give the key to the other Mac by hand instead (printed above).")
            return False
        out = (r.stderr + r.stdout).strip()
        if r.returncode != 0 and "nothing to commit" not in out and "no changes added" not in out:
            say(f"  WARN could not publish {rel} ({' '.join(cmd[:3])}: {out[-200:]}). Give the key to the other Mac by hand instead (printed above).")
            return False
    say(f"  ok   published {rel} (committed and pushed); the other Mac gets it with git pull")
    return True


def finish_link(ts, port, python, machine, local_key, peers, publish=False):
    """The last part of `--link`, once the services run: share this Mac's PUBLIC key, forward the tailnet port, test each linked Mac.

    The key comes FIRST on purpose: the other Mac needs it whatever Tailscale does, and it must not be lost behind an error. Returns normally
    when everything works. Stops with code 2 and ONE plain sentence (never a traceback) when Tailscale cannot be used, the port is already
    forwarded somewhere else, or a linked Mac answers but rejects this Mac's key. A Mac that is asleep or offline is only a warning."""
    say("linking to the other Mac(s)")
    if local_key:
        say(f"PUBLIC KEY (safe to share) of this Mac, {machine}: {local_key}")
        say(f"  saved as {peer_key_file(machine)}. The other Mac needs it. Run there (do not commit it to a public repo):")
        say(f"  python3 install.py --link --peer {machine} --peer-key {machine}={local_key}")
        if publish:
            publish_key(machine)
    try:
        ensure_serve(ts, port)
    except TailscaleError as exc:
        again = f"python3 {REPO / 'install.py'} --link" + ("" if port == DEFAULT_PORT else f" --port {port}")
        die(f"{exc} This Mac's own post box is installed and its public key is printed above; only the Tailscale step is missing, so other Macs "
            f"cannot reach this one yet. When Tailscale works, run:  {again}")
    if not verify_link(python, peers):
        die("linked, but another Mac rejected this Mac's key (see FAIL above)")


# ------------------------------------------------------------------ the steps
def install_claude_skill(dry, refresh=False):
    """Copy claude-skill/post/SKILL.md to ~/.claude/skills/post/SKILL.md (same rules as the Codex skill: an existing different copy is left alone
    unless refresh is set). Skipped when ~/.claude does not exist (Claude Code is not used on this Mac)."""
    root = home() / ".claude"
    if not root.is_dir():
        say("no ~/.claude here, so the Claude skill is not installed (fine if Claude Code is not used on this Mac)")
        return
    src, dst = REPO / "claude-skill" / "post" / "SKILL.md", root / "skills" / "post" / "SKILL.md"
    if dst.exists():
        same = dst.read_bytes() == src.read_bytes()
        if same or not refresh:
            say(f"Claude skill already there ({'identical' if same else 'differs: left as it is; --update-claude-skill replaces it with this repo copy'}): {dst}")
            return
        say(f"{'would replace' if dry else 'replacing'} the Claude skill with this repo's copy -> {dst}")
        if not dry:
            shutil.copyfile(src, dst)
        return
    say(f"{'would install' if dry else 'installing'} the Claude skill -> {dst}")
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def install_skills(dry, args):
    """The skill of every platform whose door is on."""
    if lib.doors.enabled("claude"):
        install_claude_skill(dry, args.update_claude_skill)
    if lib.doors.enabled("codex"):
        install_codex_skill(dry, args.update_codex_skill)


def install_codex_skill(dry, refresh=False):
    skills = home() / ".codex" / "skills"
    if not skills.is_dir():
        say("no ~/.codex/skills here, so the Codex skill is not installed (fine if Codex is not used on this Mac)")
        return
    src, dst = REPO / "codex-skill" / "post" / "SKILL.md", skills / "post" / "SKILL.md"
    if dst.exists():
        same = dst.read_bytes() == src.read_bytes()
        if same or not refresh:
            say(f"Codex skill already there ({'identical' if same else 'differs: left as it is; --update-codex-skill replaces it with this repo copy'}): {dst}")
            return
        say(f"{'would replace' if dry else 'replacing'} the Codex skill with this repo's copy -> {dst}")
        if not dry:
            shutil.copyfile(src, dst)
        return
    say(f"{'would install' if dry else 'installing'} the Codex skill -> {dst}")
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def link_post(dry):
    target, link = REPO / "bin" / "post", local_bin() / "post"
    if link.is_symlink() and Path(os.readlink(link)) == target:
        say(f"`post` link already correct: {link}")
    elif link.exists() and not link.is_symlink():
        die(f"{link} exists and is not a link; move it away and run again")
    else:
        say(f"{'would link' if dry else 'linking'} {link} -> {target}")
        if not dry:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink():
                link.unlink()
            link.symlink_to(target)
    if str(local_bin()) not in os.environ.get("PATH", "").split(":"):
        say(f"NOTE: {local_bin()} is not on this shell's PATH. Add this line to ~/.zshrc, then open a new terminal:")
        say(f'      export PATH="$HOME/.local/bin:$PATH"')


def uninstall(args):
    for name, plist in zip(labels(args.label_prefix), own_plists(args.label_prefix)):
        if not args.no_launchd and plist.exists():
            launchctl("bootout", domain(), str(plist))
        if plist.exists():
            plist.unlink()
            say(f"removed {plist}")
    link = local_bin() / "post"
    if link.is_symlink() and Path(os.readlink(link)) == REPO / "bin" / "post":
        link.unlink()
        say(f"removed {link}")
    ts = find_tailscale()
    forward_gone = True
    if ts and lib.load_link().get("peers"):
        forward_gone = remove_serve(ts, args.port)
    say(f"kept: the code ({REPO}), its .venv, the mailbox data ({store_dir()}), the logs in {logs_dir()}, and this Mac's login key and link list in {support_dir()}")
    if not forward_gone:
        say("uninstalled, EXCEPT the Tailscale forward: it is still in place (see WARN above), so remove it by hand")
        return 2
    say("uninstalled")
    return 0


def next_steps(args):
    say("")
    say(f"Installed. Doors on: {lib.doors.describe()}.")
    say("Left for you (this script does not edit your agent settings):")
    say('  1. Claude Code: add   "crossSessionInbound": "accept"   to ~/.claude/settings.json, so sessions accept mail the relay hands them')
    say("     (a Claude Code setting this repo does not test; without it, sessions in bypass mode may hold relay mail as from an unidentified session).")
    if lib.doors.enabled("codex"):
        say("  2. Codex: add the post rule to ~/.codex/AGENTS.md (the wording is in README.md, section Enable other doors).")
    say("To message another Mac, link the two Macs (README, section Link two Macs):  python3 install.py --link --peer <other Mac>  on BOTH.")
    say(f"Update later with:  git pull && python3 {REPO / 'install.py'}      Undo with:  python3 {REPO / 'install.py'} --uninstall")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install the post box on this Mac.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--label-prefix", default="com.postbox")
    ap.add_argument("--skip-deps", action="store_true", help="do not run brew or pip (tests)")
    ap.add_argument("--no-launchd", action="store_true", help="write the files but do not load them (tests)")
    ap.add_argument("--force", action="store_true", help="install even if another post box seems to exist")
    ap.add_argument("--link", action="store_true", help="link this Mac to the Mac(s) named with --peer (Tailscale forward plus keys)")
    ap.add_argument("--peer", action="append", default=[], metavar="NAME[=HOST[:PORT]]", help="another Mac to link to (repeat for several); its tailnet name is looked up when HOST is left out")
    ap.add_argument("--peer-key", action="append", default=[], metavar="NAME=KEY", help="save another Mac's PUBLIC key (safe to share) as peers/NAME.pub")
    ap.add_argument("--publish-key", action="store_true", help="after linking, commit and push this Mac's public key so the other Mac can pull it (best effort)")
    ap.add_argument("--machine", help="this Mac's name in messages (default: the host name)")
    ap.add_argument("--update-claude-skill", action="store_true", help="replace an existing Claude post skill with this repo's copy (otherwise a changed one is left alone)")
    ap.add_argument("--update-codex-skill", action="store_true", help="replace an existing Codex post skill with this repo's copy (only with the codex door on; otherwise a changed one is left alone)")
    args = ap.parse_args(argv)

    if sys.platform != "darwin":
        die("the installer is for macOS only")
    if sys.version_info < MIN_PYTHON:
        die(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is needed, this is {sys.version.split()[0]}")
    if args.uninstall:
        return uninstall(args)

    started = time.time()
    venv_python = REPO / ".venv" / "bin" / "python"
    if args.check:
        say("checking an existing install")
        ok = verify(args.port, 0, venv_python if venv_python.exists() else None, wait=5)
        linked = lib.load_link().get("peers") or {}
        if linked and venv_python.exists():
            say(f"checking the link to: {', '.join(sorted(linked))}")
            ts = find_tailscale()
            forwarded = None
            if ts:
                try:
                    forwarded = serve_forward(ts, args.port)
                except RuntimeError as exc:
                    say(f"  WARN cannot read Tailscale's forward list ({exc})")
            good = forwarded == f"127.0.0.1:{args.port}"
            say(f"  {'ok  ' if good else 'FAIL'} Tailscale forwards tailnet port {args.port} to the mailbox" + ("" if good else " (other Macs cannot reach this one; run install.py --link again)"))
            ok &= good
            ok &= verify_link(venv_python, linked)
        sys.exit(0 if ok else 2)

    dry = args.dry_run
    nats_bin = find_nats()
    brew = find_brew()
    if nats_bin is None and not args.skip_deps and brew is None:
        die("nats-server is not installed and Homebrew was not found. Install Homebrew (https://brew.sh), then run this again.")
    if nats_bin is None and args.skip_deps:
        die("nats-server not found (set NATS_SERVER_BIN)")
    if not args.force:
        others = other_postbox_plists(args.label_prefix)
        if others:
            die("another post box install exists on this Mac: " + ", ".join(p.name for p in others) + ". Use that one (nothing changed), or pass --force.")
        if not any(p.exists() for p in own_plists(args.label_prefix)) and port_in_use(args.port):
            die(f"something already listens on 127.0.0.1:{args.port} and it is not this install. Nothing changed; use --port or --force.")

    conf = support_dir() / "nats-server.conf"
    nats_label, relay_label = labels(args.label_prefix)
    nats_plist_path, relay_plist_path = own_plists(args.label_prefix)
    nats_url = None if args.port == DEFAULT_PORT else f"nats://127.0.0.1:{args.port}"
    host = socket.gethostname().split(".")[0].lower() or "mac"
    machine = lib.norm(args.machine or os.environ.get("POSTBOX_MACHINE") or lib.load_link().get("machine") or host)
    linking = bool(args.link or args.peer or args.peer_key)
    peers = dict(lib.load_link().get("peers") or {})
    ts = None
    if linking:
        ts = find_tailscale()
        if ts is None:
            die("linking needs Tailscale and it was not found. Install Tailscale and sign in on this Mac first (or set TAILSCALE_BIN).")
        for spec in args.peer:
            name, host_spec, port_spec = parse_peer_spec(spec)
            if name == machine:
                die(f"--peer {name} is this Mac itself")
            try:
                peers[name] = {"host": host_spec or resolve_peer_host(ts, name), "port": port_spec or DEFAULT_PORT}
            except TailscaleError as exc:
                die(f"{exc} Nothing was changed. Run the same command again once Tailscale works.")
        if not peers:
            die("--link needs the other Mac's name: --link --peer NAME")

    say(("DRY RUN, nothing will change. " if dry else "") + f"installing the post box from {REPO}")
    say(f"  NATS server binary : {nats_bin or 'will be installed with: brew install nats-server'}")
    say(f"  python environment : {venv_python} ({'exists' if venv_python.exists() else 'will be built from requirements.txt'})")
    say(f"  NATS config        : {conf}   (port {args.port}, data in {store_dir()})")
    say(f"  launchd services   : {nats_plist_path.name}, {relay_plist_path.name}")
    say(f"  doors on           : {lib.doors.describe()}   (postbox/doors.py)")
    if os.environ.get(lib.doors.ENV_NAME) is not None:
        say(f"  WARNING            : {lib.doors.ENV_NAME} is set in this shell. The installed relay does not get it and will use the list in postbox/doors.py; edit that file to change the doors for the service.")
    say(f"  command            : {local_bin() / 'post'} -> {REPO / 'bin' / 'post'}")
    seed_file = support_dir() / "machine.seed"
    say(f"  login key          : {seed_file} ({'exists' if seed_file.exists() else 'will be created'}; the private half never leaves this Mac)")
    for name in sorted(peers):
        say(f"  linked Mac         : {name} at {peers[name]['host']}:{peers[name]['port']} (its public key: {peer_key_label(name)})")
    if linking:
        say(f"  Tailscale forward  : tailnet port {args.port} -> 127.0.0.1:{args.port} (tailnet only, through {ts})")
    if dry:
        install_skills(True, args)
        link_post(True)
        return 0

    if nats_bin is None:
        try:
            subprocess.run([brew, "install", "nats-server"], check=True)
        except subprocess.CalledProcessError as exc:
            die(f"`brew install nats-server` failed (exit {exc.returncode}). Run it by hand to see why (a brand-new macOS release can lack a ready-made package for a while), then run this again.")
        nats_bin = find_nats() or die("brew installed nats-server but it cannot be found")
    if not args.skip_deps:
        try:
            if venv_python.exists() and not venv_healthy(venv_python):
                say("the python environment is broken (its Python was probably upgraded or removed): rebuilding it")
                shutil.rmtree(REPO / ".venv")
            if not venv_python.exists():
                say("building the python environment (one time)")
                subprocess.run([sys.executable, "-m", "venv", str(REPO / ".venv")], check=True)
            subprocess.run([str(venv_python), "-m", "pip", "install", "-q", "-r", str(REPO / "requirements.txt")], check=True)
        except subprocess.CalledProcessError as exc:
            die(f"could not set up the python environment ({exc.cmd[-3:]} exit {exc.returncode}). Check your network, then run this again.")

    kp = login_python(venv_python, args.skip_deps, seed_file, peers)     # may stop right here: nothing has been written yet
    for folder in (support_dir(), store_dir(), logs_dir(), agents_dir()):
        folder.mkdir(parents=True, exist_ok=True)
    local_key = keytool(kp, "ensure", str(seed_file)) if kp is not None else None
    if local_key is None:
        say("no python with the key library here (test setup, no key yet, no linked Macs): the mailbox config is written WITHOUT a login")
    for spec in args.peer_key:
        name, _, key = spec.partition("=")
        name, key = lib.norm(name), key.strip()
        taken = {}
        for other in peers:
            if other != name and read_peer_key(other):
                taken[read_peer_key(other)] = other
        check_peer_key(nats_bin, name, key, local_key, f"the key given for {name} (--peer-key)",
                       f"Nothing was saved. Copy the whole public key install.py printed on {name} and run this again with --peer-key {name}=<key>.", taken=taken)
        save_public_key(name, key)
    peer_keys = trusted_peer_keys(nats_bin, peers, local_key) if local_key else []
    if local_key and linking:
        save_public_key(machine, local_key)
    try:
        conf_text = nats_conf(f"postbox-{host}", args.port, store_dir(), local_key, peer_keys)
    except ValueError as exc:
        die(f"cannot build the mailbox config: {exc}")
    validate_conf(nats_bin, conf_text)
    write_private(conf, conf_text)
    if linking:
        (support_dir() / "link.json").write_text(json.dumps({"machine": machine, "peers": peers}, indent=2) + "\n")
    nats_plist_path.write_bytes(nats_plist(nats_label, nats_bin, conf, REPO, logs_dir() / "postbox-nats.log"))
    relay_plist_path.write_bytes(relay_plist(relay_label, venv_python, REPO / "postbox" / "relay.py", REPO, relay_path(), nats_url, logs_dir() / "postbox-relay.log"))
    say(f"wrote {conf.name}, {nats_plist_path.name}, {relay_plist_path.name}")
    link_post(False)
    install_skills(False, args)

    if args.no_launchd:
        say("--no-launchd: services not loaded")
        return 0
    for label, plist in ((nats_label, nats_plist_path), (relay_label, relay_plist_path)):
        load_service(label, plist)
    say("services loaded; verifying")
    if not verify(args.port, started, venv_python if venv_python.exists() else None):
        die("installed, but a check failed (see FAIL above). Logs: " + str(logs_dir() / "postbox-nats.log") + ", " + str(logs_dir() / "postbox-relay.log"))
    if linking:
        finish_link(ts, args.port, venv_python, machine, local_key, peers, args.publish_key)
    next_steps(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
