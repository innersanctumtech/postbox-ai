#!/usr/bin/env python3
"""The door switch (postbox/doors.py): the shipped default is Claude only; a switched-off agent is refused with `no_door`; the environment
override POSTBOX_DOORS switches a door on again. Every case runs in a fresh child process (or a throwaway HOME / mailbox), because the
other suites switch every door on (tests/_env.py) and this one checks what a real user gets. Nothing here touches the real post box.

Run: .venv/bin/python tests/test_doors.py
"""
import json
import os
import re
os.environ["POSTBOX_TELEMETRY"] = "0"
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True   # before the first helper import: that import is what would write tests/__pycache__
import _guard  # noqa: E402,F401  (never reach a real post box: see tests/_guard.py)

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
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


def clean_env(**extra):
    """The caller's environment without anything that could change who is sending or which doors are on."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "GROK", "POSTBOX_"))}
    env["POSTBOX_TELEMETRY"] = "0"
    env["POSTBOX_NATS_URL"] = os.environ["POSTBOX_NATS_URL"]   # a port nothing listens on (set by _guard); a child that forgets its own mailbox fails closed
    env.update(extra)
    return env


def child(code, **extra):
    """Run python code in a fresh process with the postbox folder importable; returns (exit code, stdout, stderr)."""
    script = f"import sys; sys.path.insert(0, {str(ROOT / 'postbox')!r})\n" + code
    r = subprocess.run([PY, "-c", script], env=clean_env(**extra), capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


# ------------------------------------------------------------------ the switch itself
print("the switch")
text = (ROOT / "postbox" / "doors.py").read_text()
lines = [ln.strip() for ln in text.splitlines()]
check("shipped list: claude is the only active line", any(ln.startswith('"claude"') for ln in lines)
      and not any(ln.startswith(('"codex"', '"grok"', '"grokbot-desktop"')) for ln in lines))
check("codex, grok and grokbot-desktop lines are present but commented out",
      all(any(ln.startswith(f'# "{name}"') for ln in lines) for name in ("codex", "grok", "grokbot-desktop")))
code, out, err = child("import doors; print(sorted(doors.enabled_names()))")
check("default: only claude is enabled", code == 0 and out == "['claude']", out + err)
code, out, err = child("import doors; print(doors.enabled('claude'), doors.enabled('codex'), doors.enabled('grok'), doors.enabled('grokbot-desktop'), doors.enabled('gemini'))")
check("default: enabled() says yes for claude, no for everything else", out == "True False False False False", out + err)
code, out, err = child("import doors; print(sorted(doors.enabled_names()))", POSTBOX_DOORS="claude,codex")
check("POSTBOX_DOORS=claude,codex enables codex", out == "['claude', 'codex']", out + err)
code, out, err = child("import doors; print(sorted(doors.enabled_names()))", POSTBOX_DOORS=" Claude , GROK ,, ")
check("POSTBOX_DOORS ignores case, spaces and empty items", out == "['claude', 'grok']", out + err)
code, out, err = child("import doors; print(sorted(doors.enabled_names()))", POSTBOX_DOORS="")
check("POSTBOX_DOORS set to nothing means no door at all (an explicit choice, not a fall back)", out == "[]", out + err)
code, out, err = child("import doors; print(doors.describe())")
check("describe() names what is on and what is off", out == "claude (off: codex, grok, grokbot-desktop)", out + err)

print("the code follows the switch")
code, out, err = child("import lib; print(sorted(lib.DOORS))")
check("lib.DOORS holds only the claude door by default", out == "['claude']", out + err)
code, out, err = child("import lib; print(sorted(lib.DOORS))", POSTBOX_DOORS="claude,codex,grok,grokbot-desktop")
check("lib.DOORS holds all four doors when all are switched on", out == "['claude', 'codex', 'grok', 'grokbot-desktop']", out + err)
code, out, err = child("""
import lib
from unittest import mock
def boom(*a, **k): raise AssertionError('looked for a switched-off agent')
with mock.patch.object(lib, 'codex_sessions', boom), mock.patch.object(lib, 'grok_sessions', boom), mock.patch.object(lib, 'claude_sessions', lambda: ['c']):
    print(lib.all_sessions())
""")
check("all_sessions() never looks for Codex or Grok sessions by default", out == "['c']", out + err)
code, out, err = child("""
import lib
from unittest import mock
with mock.patch.object(lib, 'codex_sessions', lambda table=None: ['x']), mock.patch.object(lib, 'grok_sessions', lambda: ['g']), mock.patch.object(lib, 'claude_sessions', lambda: ['c']):
    print(lib.all_sessions())
""", POSTBOX_DOORS="claude,codex,grok")
check("with the doors on, all_sessions() lists them too", out == "['c', 'x', 'g']", out + err)
code, out, err = child("""
import lib
print(lib.my_identity(), lib.IDENTITY_TRACE['failed'])
""", CODEX_THREAD_ID="019a0000-0000-7000-8000-000000000000", GROK_AGENT="1")
check("default: a Codex or Grok marker in the environment is ignored (no look for either)", out == "None []", out + err)
code, out, err = child("""
import lib
print(lib.my_identity() is None, lib.IDENTITY_TRACE['failed'])
""", CODEX_THREAD_ID="019a0000-0000-7000-8000-000000000000", POSTBOX_DOORS="claude,codex")
check("with the codex door on, the same marker is looked at (and its failure recorded)", out.startswith("True") and "codex_thread" in out, out + err)

print("help text")
r = subprocess.run([PY, str(ROOT / "postbox" / "post.py"), "--help"], env=clean_env(), capture_output=True, text=True, timeout=60)
check("post --help ends with the doors that are on", r.returncode == 0 and "Doors on: claude (off: codex, grok, grokbot-desktop)" in r.stdout, r.stdout[-300:] + r.stderr)
check("post --help does not offer wake-test while grokbot-desktop is off", "wake-test" not in r.stdout)
r = subprocess.run([PY, str(ROOT / "postbox" / "post.py"), "--help"], env=clean_env(POSTBOX_DOORS="claude,grokbot-desktop"), capture_output=True, text=True, timeout=60)
check("post --help offers wake-test once grokbot-desktop is on", "wake-test" in r.stdout and "Doors on: claude, grokbot-desktop" in r.stdout, r.stdout[-300:])
r = subprocess.run([PY, str(ROOT / "postbox" / "post.py"), "wake-test", "grokbot-desktop@project-a"], env=clean_env(), capture_output=True, text=True, timeout=60)
check("post wake-test with the door off: refused in words, no webhook call", r.returncode == 2 and "switched off" in r.stderr, r.stderr)

# ------------------------------------------------------------------ the installer follows the switch
print("installer")


def sandbox(tmp):
    home, bindir = Path(tmp) / "home", Path(tmp) / "bin"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex" / "skills").mkdir(parents=True)
    bindir.mkdir()
    stub = bindir / "nats-server"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return home, {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", "NATS_SERVER_BIN": str(stub), "POSTBOX_STORE_DIR": str(Path(tmp) / "store"),
                  "POSTBOX_TELEMETRY": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def install(env, *args):
    r = subprocess.run([PY, str(ROOT / "install.py"), "--skip-deps", *args], env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


with tempfile.TemporaryDirectory() as tmp:
    home, env = sandbox(tmp)
    code, out = install(env, "--dry-run", "--port", str(free_port()))
    check("install --dry-run (default doors): mentions the Claude skill", code == 0 and "Claude skill" in out and "would install the Claude skill" in out, out[-500:])
    check("install --dry-run (default doors): no Codex skill, though ~/.codex/skills exists", "Codex skill" not in out and ".codex" not in out, out)
    check("install --dry-run (default doors): names the doors that are on", "claude (off: codex, grok, grokbot-desktop)" in out, out)
    check("install --dry-run changed nothing", not (home / ".claude" / "skills").exists() and not (home / ".local").exists())
    check("install --dry-run without POSTBOX_DOORS: no warning about it", "WARNING" not in out, out)
with tempfile.TemporaryDirectory() as tmp:
    home, env = sandbox(tmp)
    code, out = install(dict(env, POSTBOX_DOORS="claude,codex"), "--dry-run", "--port", str(free_port()))
    check("install with POSTBOX_DOORS set warns that the installed relay will not get it", code == 0 and "WARNING" in out and "POSTBOX_DOORS is set" in out and "postbox/doors.py" in out, out[-600:])
doors_doc = " ".join((ROOT / "postbox" / "doors.py").read_text().split())
check("doors.py does not promise a one-command override the installed relay cannot honour", "for one command" not in doors_doc and "installed relay" in doors_doc and "no_door" in doors_doc)
with tempfile.TemporaryDirectory() as tmp:
    home, env = sandbox(tmp)
    env_all = dict(env, POSTBOX_DOORS="claude,codex")
    code, out = install(env_all, "--dry-run", "--port", str(free_port()))
    check("install --dry-run with the codex door on: mentions both skills", code == 0 and "Claude skill" in out and "Codex skill" in out, out[-500:])
with tempfile.TemporaryDirectory() as tmp:
    home, env = sandbox(tmp)
    code, out = install(env, "--no-launchd", "--port", "47999")
    claude_skill = home / ".claude" / "skills" / "post" / "SKILL.md"
    check("install (default doors) copies the Claude skill into ~/.claude/skills/post",
          code == 0 and claude_skill.is_file() and claude_skill.read_bytes() == (ROOT / "claude-skill" / "post" / "SKILL.md").read_bytes(), out[-500:])
    check("install (default doors) does not install the Codex skill, though ~/.codex/skills exists", not (home / ".codex" / "skills" / "post").exists())
    claude_skill.write_text("someone's own edit")
    code, out = install(env, "--no-launchd", "--port", "47999", "--force")
    check("an existing, different Claude skill is left alone by a normal install", claude_skill.read_text() == "someone's own edit" and "left as it is" in out, out[-300:])
    code, out = install(env, "--no-launchd", "--port", "47999", "--force", "--update-claude-skill")
    check("--update-claude-skill replaces it with this repo's copy", claude_skill.read_bytes() == (ROOT / "claude-skill" / "post" / "SKILL.md").read_bytes(), out[-300:])
    plist = (home / "Library" / "LaunchAgents" / "com.postbox.relay.plist").read_text()
    path_match = re.search(r"<key>PATH</key>\s*<string>(.*?)</string>", plist, re.S)
    relay_path = path_match.group(1) if path_match else ""
    check("the relay's launchd PATH does not carry codex's folder when codex is off", path_match is not None and "codex" not in relay_path.lower(), plist)
skill_text = (ROOT / "claude-skill" / "post" / "SKILL.md").read_text()
check("the Claude skill has its front matter and names the post command", skill_text.startswith("---\nname: post\n") and "post claude" in skill_text)

# ------------------------------------------------------------------ the whole path: a real mailbox and a real relay
print("mailbox and relay (default doors)")


def wait_for(fn, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


FAKE = r'''
import os, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(os.path.join(sys.argv[2], str(os.getpid()) + ".sock")); s.listen(5)
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
    open(sys.argv[3], "ab").write(data)
'''

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "postbox"))
os.environ.pop("POSTBOX_DOORS", None)
import install as installer  # noqa: E402

NATS_BIN = installer.find_nats()
if not NATS_BIN:
    print("  skip (nats-server not installed)")
else:
    procs = []
    TMP = Path(tempfile.mkdtemp(prefix="pbd", dir="/tmp"))
    port = free_port()
    support, socks, sessions = TMP / "support", TMP / "socks", TMP / "sessions"
    for d in (support, socks, sessions, TMP / "proj"):
        d.mkdir(parents=True)
    (TMP / "n.conf").write_text(installer.nats_conf("postbox-doors-test", port, TMP / "store", None))
    nats = subprocess.Popen([NATS_BIN, "-c", str(TMP / "n.conf")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(nats)
    try:
        assert wait_for(lambda: installer.port_in_use(port)), "the mailbox did not start"
        base = clean_env(POSTBOX_NATS_URL=f"nats://127.0.0.1:{port}", POSTBOX_SUPPORT_DIR=str(support), POSTBOX_CLAUDE_SOCK_DIR=str(socks),
                         POSTBOX_CLAUDE_SESSIONS_DIR=str(sessions), POSTBOX_MACHINE="mac-a", POSTBOX_CODEX_WATCH=str(TMP / "codex-watch.json"),
                         POSTBOX_HELD_DIR=str(TMP / "held"), POSTBOX_INBOX_DIR=str(TMP / "inbox"))

        def start_relay(env, tag):
            log = TMP / f"relay-{tag}.log"
            proc = subprocess.Popen([PY, str(ROOT / "postbox" / "relay.py")], env=env, stdout=open(log, "w"), stderr=subprocess.STDOUT)
            procs.append(proc)
            assert wait_for(lambda: "relay_ready" in log.read_text()), f"relay not ready: {log.read_text()[-300:]}"
            return proc

        frames = TMP / "claude.frames"
        (TMP / "sender").mkdir()
        fakes = []
        for folder, out_file in (("proj", frames), ("sender", TMP / "sender.frames")):   # the receiver, and a second session that does the sending
            fake = subprocess.Popen([PY, "-c", FAKE, "claude-fake-session", str(socks), str(out_file)], cwd=TMP / folder, stdout=subprocess.PIPE, text=True)
            procs.append(fake)
            fake.stdout.readline()
            fakes.append(fake)
        sock = socks / f"{fakes[1].pid}.sock"   # post runs as the sender session

        def post(env, *args):
            env = dict(env, CLAUDECODE="1", CLAUDE_CODE_MESSAGING_SOCKET=str(sock))
            r = subprocess.run([PY, str(ROOT / "postbox" / "post.py"), *args], env=env, capture_output=True, text=True, timeout=60)
            return r.returncode, (r.stdout + r.stderr).strip()

        relay = start_relay(base, "default")
        code, out = post(base, "claude@proj", "hello claude")
        check("default doors: mail to a Claude session is delivered (exit 0) and reaches that session", code == 0 and out.startswith("delivered")
              and wait_for(lambda: frames.exists() and b"hello claude" in frames.read_bytes(), 5), out)
        code, out = post(base, "codex@proj", "hello codex")
        check("default doors: mail to codex is refused with no_door (exit 4)", code == 4 and out.startswith("refused") and "no delivery door for agent 'codex'" in out, f"{code} {out}")
        code, out = post(base, "grok@proj", "hello grok")
        check("default doors: mail to grok is refused too", code == 4 and out.startswith("refused") and "grok" in out, f"{code} {out}")
        code, out = post(base, "grokbot-desktop@proj", "hello desk")
        check("default doors: mail to grokbot-desktop is refused too", code == 4 and out.startswith("refused"), f"{code} {out}")
        relay.terminate()
        relay.wait(timeout=10)

        env_claude = dict(base, POSTBOX_DOORS="claude")
        relay = start_relay(env_claude, "claude")
        code, out = post(env_claude, "codex@proj", "again")
        check("POSTBOX_DOORS=claude: post to codex => refused no_door", code == 4 and out.startswith("refused") and "no delivery door" in out, f"{code} {out}")
        relay.terminate()
        relay.wait(timeout=10)

        env_codex = dict(base, POSTBOX_DOORS="claude,codex")
        relay = start_relay(env_codex, "codex")
        code, out = post(env_codex, "codex@proj", "now with the door on")
        check("POSTBOX_DOORS=claude,codex: the same post is no longer refused (no Codex is open, so it waits: queued, exit 3)",
              code == 3 and out.startswith("queued"), f"{code} {out}")
    finally:
        for proc in procs:
            try:
                proc.terminate()
            except OSError:
                pass
        time.sleep(0.5)
        shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
