#!/usr/bin/env python3
"""End-to-end test of per-session addressing through REAL mailboxes and REAL relays: two emulated Macs (alpha, beta), each with its own
nats-server, relay and fake Claude sessions (a process with a message socket, plus Claude Code's own per-session name file).
Proves the headers survive the mailbox and the cross-Mac hop, that a message to a project with two sessions is held (not sent to both),
that the printed `post pick` command sends the held message to exactly one session, and that a printed reply command reaches exactly the sender.
Nothing here touches this Mac's real post box, launchd or Tailscale.

Run: .venv/bin/python tests/test_session_addressing_e2e.py
"""
import contextlib
import json
import os
os.environ["POSTBOX_TELEMETRY"] = "0"
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _env  # noqa: E402,F401  (every door on for this suite)
sys.path.insert(0, str(ROOT / "postbox"))
import install  # noqa: E402

os.environ["POSTBOX_MACHINE"] = "alpha"
PY = sys.executable
POST, RELAY, KEYTOOL = (str(ROOT / "postbox" / n) for n in ("post.py", "relay.py", "keytool.py"))
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
    sys.exit(0)

procs = []
TMP = tempfile.mkdtemp(prefix="pbs", dir="/tmp")


def make_mac(name):
    mac = SimpleNamespace(name=name, dir=Path(TMP) / name, port=free_port())
    mac.support, mac.socks, mac.sessions = mac.dir / "support", mac.dir / "socks", mac.dir / "sessions"
    for d in (mac.support, mac.socks, mac.sessions):
        d.mkdir(parents=True)
    mac.pub = subprocess.run([PY, KEYTOOL, "ensure", str(mac.support / "machine.seed")], capture_output=True, text=True).stdout.strip()
    mac.env = dict(os.environ, POSTBOX_NATS_URL=f"nats://127.0.0.1:{mac.port}", POSTBOX_SUPPORT_DIR=str(mac.support), POSTBOX_CLAUDE_SOCK_DIR=str(mac.socks),
                   POSTBOX_CLAUDE_SESSIONS_DIR=str(mac.sessions), POSTBOX_FORWARD_RETRY_S="2")
    for gone in ("CODEX_THREAD_ID", "POSTBOX_MACHINE"):
        mac.env.pop(gone, None)
    return mac


def boot(mac, trusted, others):
    (mac.dir / "n.conf").write_text(install.nats_conf(f"postbox-{mac.name}", mac.port, mac.dir / "store", mac.pub, [(t.name, t.pub) for t in trusted]))
    mac.nats = subprocess.Popen([NATS_BIN, "-c", str(mac.dir / "n.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(mac.nats)
    assert wait_for(lambda: install.port_in_use(mac.port)), f"{mac.name}: the mailbox did not start"
    (mac.support / "link.json").write_text(json.dumps({"machine": mac.name, "peers": {o.name: {"host": "127.0.0.1", "port": o.port} for o in others}}))
    mac.log = mac.dir / "relay.log"
    mac.relay = subprocess.Popen([PY, RELAY], env=mac.env, stdout=open(mac.log, "w"), stderr=subprocess.STDOUT)
    procs.append(mac.relay)
    assert wait_for(lambda: "relay_ready" in mac.log.read_text()), f"{mac.name}: relay not ready: {mac.log.read_text()[-300:]}"


def fake_claude(mac, project, name):
    """A Claude Code look-alike in a folder called `project`, with the per-session file Claude Code writes (its name is `name`)."""
    folder = mac.dir / project
    folder.mkdir(exist_ok=True)
    out = mac.dir / f"{project}-{name}.frames"
    proc = subprocess.Popen([PY, "-c", FAKE, "claude-fake-session", str(mac.socks), str(out)], cwd=folder, stdout=subprocess.PIPE, text=True)
    procs.append(proc)
    proc.stdout.readline()
    proc.sock, proc.out, proc.name = mac.socks / f"{proc.pid}.sock", out, name
    (mac.sessions / f"{proc.pid}.json").write_text(json.dumps({"pid": proc.pid, "sessionId": f"{abs(hash(name)) % 10**8:08d}-1111-4222-8333-{proc.pid:012d}", "name": name,
                                                              "nameSource": "user", "startedAt": int(time.time() * 1000) - 2000, "cwd": str(folder)}))
    return proc


def frames(session):
    return session.out.read_bytes().decode(errors="replace") if session.out.exists() else ""


def post(mac, sender, *args, timeout=60):
    env = dict(mac.env, CLAUDECODE="1", CLAUDE_CODE_MESSAGING_SOCKET=str(sender.sock))
    r = subprocess.run([PY, POST, *args], env=env, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def quiet(*sessions, seconds=1.5):
    time.sleep(seconds)
    return all(not frames(s) for s in sessions)


try:
    alpha, beta = make_mac("alpha"), make_mac("beta")
    boot(alpha, [beta], [beta])
    boot(beta, [alpha], [alpha])
    sender = fake_claude(alpha, "proj-s", "sender-s")
    one, two = fake_claude(alpha, "proj-p", "first-one"), fake_claude(alpha, "proj-p", "Second-Two")
    b_one, b_two = fake_claude(beta, "proj-b", "bee-one"), fake_claude(beta, "proj-b", "bee-two")

    print("-- a project with two sessions, no name: held, delivered to NOBODY")
    code, out, err = post(alpha, sender, "claude@proj-p", "please run the tests")
    check("exit 5 and a 'held:' result line", code == 5 and out.startswith("held:"), f"{code} {out} {err}")
    check("the line lists both session names and says nothing was delivered", "first-one" in out and "second-two" in out and "Nothing was delivered" in out, out)
    check("neither session received anything", quiet(one, two), frames(one) + frames(two))
    cmds = re.findall(r"^\s+(post pick \S+ \S+)$", out, re.M)
    check("one ready command per session", len(cmds) == 2 and all("#" in c for c in cmds), str(cmds))
    code, out2, err2 = post(alpha, sender, "held")
    check("`post held` shows the held message and its commands later", code == 0 and "please run the tests" in out2 and cmds[0] in out2, out2)

    print("-- the printed command sends it on to exactly one session, without retyping")
    pick = next(c for c in cmds if c.endswith("#second-two"))
    code, out, err = post(alpha, sender, *pick.split()[1:])
    check("pick: delivered", code == 0 and out.startswith("delivered:") and "second-two" in out, f"{code} {out} {err}")
    check("only the chosen session got it", wait_for(lambda: "please run the tests" in frames(two)) and not frames(one), frames(one) + "|" + frames(two))
    check("it shows the ORIGINAL sender and its session name, and the reply command targets that session",
          "Message from claude@proj-s#sender-s (post box):" in frames(two) and 'post claude@proj-s#sender-s' in frames(two), frames(two)[:300])
    code, out, err = post(alpha, sender, *pick.split()[1:])
    check("running the same pick again is refused (no double delivery)", code == 4 and "no held message" in out + err, f"{code} {out} {err}")

    print("-- reply goes to exactly the sender")
    other_sender = fake_claude(alpha, "proj-s", "other-s")
    reply = re.search(r"post (\S+) \\\"your reply\\\"", frames(two)).group(1)
    code, out, err = post(alpha, two, reply, "thanks, done")
    check("the reply, with the address exactly as printed, is delivered", code == 0 and out.startswith("delivered:"), f"{code} {out} {err}")
    check("the sender got it, the other session in the sender's project did NOT", wait_for(lambda: "thanks, done" in frames(sender)) and not frames(other_sender), frames(other_sender))

    print("-- a name, any case")
    code, out, err = post(alpha, sender, "claude@proj-p#FIRST-ONE", "named, upper case")
    check("delivered to the named session only", code == 0 and wait_for(lambda: "named, upper case" in frames(one)) and "named, upper case" not in frames(two), f"{code} {out}")
    code, out, err = post(alpha, sender, "claude@proj-p#nosuch", "UNKNOWN-NAME-MSG")
    check("an unknown name: refused (exit 4) with the current names, nothing delivered", code == 4 and "first-one" in out and "second-two" in out
          and "UNKNOWN-NAME-MSG" not in frames(one) + frames(two), f"{code} {out}")
    code, out, err = post(alpha, sender, "claude@proj-p#", "x")
    check("a # with no name: exit 2 before anything is sent", code == 2 and "bad session name" in err, f"{code} {err}")
    code, out, err = post(alpha, sender, "who")
    check("post who shows the names", "#first-one" in out and "#second-two" in out and "#sender-s" in out, out)

    print("-- across Macs: agent@project@machine#name")
    code, out, err = post(alpha, sender, "claude@proj-b@beta", "which of you?")
    check("two sessions on the other Mac, no name: held there and the bounce comes back as a normal result", code == 5 and out.startswith("held:") and "bee-one" in out and "bee-two" in out, f"{code} {out} {err}")
    cross = re.findall(r"^\s+(post pick \S+ \S+)$", out, re.M)
    check("the commands carry the machine, then the name: claude@proj-b@beta#name", cross and all(re.search(r"claude@proj-b@beta#bee-(one|two)$", c) for c in cross), str(cross))
    check("nobody on beta received it", quiet(b_one, b_two))
    code, out, err = post(alpha, sender, *next(c for c in cross if c.endswith("bee-two")).split()[1:])
    check("pick across Macs: delivered to exactly that session", code == 0 and wait_for(lambda: "which of you?" in frames(b_two)) and not frames(b_one), f"{code} {out} {err}")
    check("beta's session sees the sender as agent@project@machine#name", "Message from claude@proj-s@alpha#sender-s (post box):" in frames(b_two), frames(b_two)[:300])
    code, out, err = post(alpha, sender, "claude@proj-b@beta#bee-one", "direct to bee-one")
    check("named, across Macs: delivered to that session only", code == 0 and wait_for(lambda: "direct to bee-one" in frames(b_one)) and "direct to bee-one" not in frames(b_two), f"{code} {out} {err}")
    reply = re.search(r"post (\S+) \\\"your reply\\\"", frames(b_one)).group(1)
    check("the reply command printed on beta is agent@project@machine#name", reply == "claude@proj-s@alpha#sender-s", reply)
    code, out, err = post(beta, b_one, reply, "answer from beta")
    check("and running it reaches only the sender on alpha", code == 0 and wait_for(lambda: "answer from beta" in frames(sender)) and "answer from beta" not in frames(other_sender), f"{code} {out} {err}")
    code, out, err = post(alpha, sender, "who", "beta")
    check("post who beta lists names in the typeable form", "claude@proj-b@beta#bee-one" in out and "claude@proj-b@beta#bee-two" in out, out)

    print("-- mail that waited for an empty project, then finds two sessions")
    code, out, err = post(alpha, sender, "claude@proj-q", "waited for you")
    check("nobody there: queued as before (exit 3)", code == 3 and out.startswith("queued:"), f"{code} {out}")
    q_one, q_two = fake_claude(alpha, "proj-q", "quiet-one"), fake_claude(alpha, "proj-q", "quiet-two")
    check("when two sessions appear it is NOT delivered to both; the sender's session is told it was held, with the ready commands",
          wait_for(lambda: "Post box notice" in frames(sender) and "waited" not in frames(q_one) + frames(q_two) and "quiet-two" in frames(sender), 40), frames(sender)[-500:] + "|" + frames(q_one))
    late = re.findall(r"post pick (\S+) claude@proj-q#quiet-two", frames(sender))
    check("the notice carries a working pick command", bool(late))
    if late:
        code, out, err = post(alpha, sender, "pick", late[-1], "claude@proj-q#quiet-two")
        check("running it delivers the waiting message to that one session", code == 0 and wait_for(lambda: "waited for you" in frames(q_two)) and not frames(q_one), f"{code} {out} {err}")
finally:
    for p in procs:
        with contextlib.suppress(Exception):
            p.terminate()
    for p in procs:
        with contextlib.suppress(Exception):
            p.wait(timeout=5)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
