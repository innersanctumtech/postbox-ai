"""Keeps the guarded test suites away from a real post box. Imported first by tests/_env.py (and by suites that do not use _env). Two suites do not import it, see README.md "Tests".

A post box that is installed and running on the machine listens on the default address, nats://127.0.0.1:4222. A test that sends through
that address delivers a real message into a real session. So, before any other code is imported:

  1. If POSTBOX_NATS_URL is already set to the default address (or to port 4222 of this Mac under any name), the process exits at once.
  2. If it is not set, it is set to a port that nothing listens on, so a test that forgets its own mailbox fails to connect instead of
     reaching the real one. A test that wants a mailbox starts its own on a spare port and sets POSTBOX_NATS_URL itself.
  3. POSTBOX_SUPPORT_DIR defaults to a throwaway folder, so no test reads or writes the real support folder by accident.
  4. Whatever the environment says (a test may wipe POSTBOX_* variables later), any connection from THIS process to port 4222 on this
     Mac raises BrokerLeak, whatever way the Mac is named (127.0.0.1, localhost, 127.1, ::ffff:127.0.0.1, a name that resolves to
     loopback). BrokerLeak is a BaseException, so a broad `except Exception` cannot hide it.
  5. The same guard is switched on in every Python process this process starts, even when the caller builds the child's environment by
     hand, wipes every POSTBOX_* variable, or passes an empty one: every launch API (subprocess.Popen, so run/check_output/asyncio
     subprocesses too; os.exec*/os.spawn*/os.posix_spawn*/os.system; the C function multiprocessing uses) is wrapped to vet the command
     and to add PYTHONPATH=tests/_guard_site (its sitecustomize.py loads this file) and the marker PBGUARD_ACTIVE (not a POSTBOX_ name,
     so wiping POSTBOX_* keeps it). A child that names the default address in its environment is refused before it starts. A launch is
     also refused when the guard could not follow it: Python with -I, -S or -E (parsed from the real command line grammar, with
     `executable=`, shebang lines and env/sh wrappers resolved), env that clears or rewrites the guard variables, a shell command with
     operators or a shell script that unsets them, and any program that is not on the short allowed list (ALLOWED_TOOLS): nc, curl,
     perl or a compiled client could reach the post box without ever loading this guard.
  6. The whole test process runs under sandbox-exec with a profile that makes the operating system itself refuse connections to the
     protected ports (kernel_profile). The profile is inherited by every descendant, so it also stops what rule 5 cannot see: native code
     (ctypes, an extension module), a shell script that clears the environment, a compiled client. This layer is REQUIRED and it fails
     closed: a process that is not under the profile re-starts itself under it; if that cannot be done (not macOS, no sandbox-exec, this
     process is already inside another sandbox, which forbids a second one) the process exits with code 3 and says why. No environment
     variable is believed: whether the profile is in force is decided by trying, in this very process, a connection to a canary port
     (CANARY_PORT) that the profile denies and that normally refuses (kernel_enforced): the operating system answers EPERM only under a
     profile. There is no switch that skips this layer (PBGUARD_KERNEL is not read any more).
  7. The protected set is port 4222, every port the post box on this Mac is installed on (read-only from the launchd job files in
     ~/Library/LaunchAgents: POSTBOX_NATS_URL of a relay, -p/--port of a nats-server; installed_ports), and PBGUARD_PORTS=<port>[,<port>]
     (it can only make the guard stricter); tests/test_env_guard.py uses it to prove every rule above against a harmless stand-in
     listener instead of the real post box. The profile names `localhost`, which the operating system matches for every address of this
     Mac (127.0.0.1, ::1, 0.0.0.0, its LAN and tailnet addresses); tests/test_env_guard.py proves that.
  What the guard does NOT do: stop a connection to port 4222 on ANOTHER host; protect a port that is not 4222, not found in a launchd job
  file and not in PBGUARD_PORTS (a post box started by hand on another port); protect a port named in PBGUARD_PORTS that was set only
  after the process tree started (the profile is fixed when the tree starts; the Python layer still covers it); or stop a process that is
  not started from a test (the guard only exists in processes the suites start).
"""
import functools
import errno
import ipaddress
import os
import plistlib
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True   # running the suites must not leave __pycache__ folders in the tree that gets published

DEFAULT_PORT = 4222
LOCAL_NAMES = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


class BrokerLeak(BaseException):
    """A test tried to reach the post box on the default port."""


def url_port(url):
    """The port a NATS url names (the default 4222 when it names none), and whether the host is this Mac."""
    m = re.match(r"^(?:[a-z]+://)?(?:[^@/]*@)?(\[[^\]]*\]|[^:/]*)(?::(\d+))?", str(url).strip(), re.I)
    host = (m.group(1) if m else "").strip("[]").lower()
    return (int(m.group(2)) if m and m.group(2) else DEFAULT_PORT), host in LOCAL_NAMES


MARKER = "PBGUARD_ACTIVE"          # set in every child; not a POSTBOX_ name, so a child that wipes POSTBOX_* keeps it
PORTS_VAR = "PBGUARD_PORTS"
REEXEC_VAR = "PBGUARD_REEXEC"      # set just before this process re-starts itself under sandbox-exec; only ever makes the guard STRICTER (see _enter_kernel_layer)
PLIST_DIRS_VAR = "PBGUARD_PLIST_DIRS"   # extra folders of launchd job files to read for installed ports (added to ~/Library/LaunchAgents, never instead of it)
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
CANARY_PORT = 1   # tcpmux: nothing listens there; the kernel profile denies it too, so a connection that gets EPERM (not ECONNREFUSED) proves the profile is in force
SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_guard_site")
_real_getaddrinfo = socket.getaddrinfo
_connect_ex = socket.socket.connect_ex
_REAL_CONNECT_EX = getattr(_connect_ex, "_pbguard_real", _connect_ex)   # the unpatched method, whichever module copy patched it first
PROTECTED = {DEFAULT_PORT}
_applying_kernel_layer = False   # True only while _enter_kernel_layer itself starts sandbox-exec (a process-internal flag, not an environment variable)


def _protected_from_env():
    ports = {DEFAULT_PORT}
    for part in os.environ.get(PORTS_VAR, "").split(","):
        if part.strip().isdigit():
            ports.add(int(part))
    return ports


def _loopback(ip_text):
    try:
        ip = ipaddress.ip_address(str(ip_text).split("%")[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


def is_local_host(host, resolve=False):
    """True when `host` is this Mac under any spelling: the listed names, 127.1 and other short or hex forms, ::ffff:127.0.0.1, and (when
    `resolve` is on) any name that resolves to a loopback address."""
    host = str(host).strip("[]").lower().rstrip(".")
    if host in LOCAL_NAMES or host.endswith(".localhost") or _loopback(host):
        return True
    if re.fullmatch(r"[0-9a-fx.]+", host):
        try:
            return _loopback(socket.inet_ntoa(socket.inet_aton(host)))
        except OSError:
            pass
    if resolve and host:
        try:
            return any(_loopback(info[4][0]) for info in _real_getaddrinfo(host, None))
        except (OSError, UnicodeError):
            return False
    return False


def _url_local_port(url):
    """The port `url` names when its host is this Mac under any spelling, else None."""
    port, local = url_port(url)
    rest = re.sub(r"^[a-z]+:[/]{2}", "", str(url).strip(), flags=re.I).split("/")[0].rsplit("@", 1)[-1]
    host = rest[:rest.index("]") + 1] if rest.startswith("[") and "]" in rest else rest.split(":")[0]
    return port if (local or is_local_host(host)) else None


def names_real_broker(url):
    """True when `url` points at a protected port (4222, plus the installed ports and PBGUARD_PORTS) on this Mac."""
    return _url_local_port(url) in PROTECTED


def _plist_ports(data):
    """The local ports a launchd job file says a post box is reached on: POSTBOX_NATS_URL in the job's environment (what the installer writes
    for a relay on a port other than 4222), and -p/--port on a nats-server command line."""
    ports = set()
    if not isinstance(data, dict):
        return ports
    env = data.get("EnvironmentVariables")
    url = env.get("POSTBOX_NATS_URL") if isinstance(env, dict) else None
    if isinstance(url, str) and url.strip():
        port = _url_local_port(url)
        if port is not None:
            ports.add(port)
    args = data.get("ProgramArguments")
    if isinstance(args, list) and args and "nats-server" in os.path.basename(str(args[0])):
        words = [str(a) for a in args]
        for i, word in enumerate(words):
            value = word.split("=", 1)[1] if word.startswith(("--port=", "-p=")) else (words[i + 1] if word in ("-p", "--port") and i + 1 < len(words) else "")
            if value.isdigit() and 0 < int(value) < 65536:
                ports.add(int(value))
    return ports


def _launch_agent_dirs():
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir      # the account's real home, whatever $HOME says (a test may point HOME at a throwaway folder)
    except KeyError:
        home = os.path.expanduser("~")
    return [os.path.join(home, "Library", "LaunchAgents"), *[d for d in os.environ.get(PLIST_DIRS_VAR, "").split(os.pathsep) if d]]


def installed_ports():
    """(ports, problems): every port a post box installed on this Mac listens on or is reached at, read-only from the launchd job files
    (nothing is asked of launchd and no support folder is opened). `problems` names each job file that mentions a post box but cannot be
    read or parsed, so a custom port the guard could not learn about is reported by the caller, never silently dropped."""
    ports, problems = set(), []
    for folder in _launch_agent_dirs():
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue                                  # no such folder: nothing is installed there
        for name in names:
            if not name.endswith(".plist"):
                continue
            path = os.path.join(folder, name)
            ours = "postbox" in name.lower()
            try:
                with open(path, "rb") as f:
                    raw = f.read(1 << 20)
            except OSError as exc:
                if ours:
                    problems.append(f"{path}: cannot be read ({exc.strerror or exc})")
                continue
            ours = ours or b"POSTBOX_NATS_URL" in raw or b"nats-server" in raw
            try:
                ports |= _plist_ports(plistlib.loads(raw))
            except Exception as exc:                  # noqa: BLE001 - plistlib raises several unrelated types for bad input
                if ours:
                    problems.append(f"{path}: cannot be parsed ({type(exc).__name__})")
    return ports, problems


def kernel_profile(ports):
    """The sandbox-exec profile that denies outbound connections to the protected ports on this Mac. The one exception is not about the
    network: /bin/ps is a setuid program, which the operating system will not start inside a sandbox at all, and postbox/lib.py asks it about
    process ids; it is let out of the sandbox by exact path (a process listing tool that opens no sockets). CANARY_PORT is denied as well
    so that a process can tell, by trying, that this profile is in force (kernel_enforced)."""
    return ("(version 1)(allow default)" + "".join(f'(deny network-outbound (remote ip "localhost:{p}"))' for p in sorted({*ports, CANARY_PORT}))
            + '(allow process-exec* (literal "/bin/ps") (with no-sandbox))')


def refuse_default(url):
    """Exit (hard) when `url` is the real post box's address."""
    if names_real_broker(url):
        sys.stderr.write(f"tests refuse to run: POSTBOX_NATS_URL={url!r} is the default post box address (127.0.0.1:{DEFAULT_PORT}); "
                         "a test would deliver real mail. Unset it; tests start their own mailbox on a spare port.\n")
        sys.exit(3)


def spare_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def child_env(env=None):
    """The environment a child process gets: what the caller built (os.environ when none), plus what keeps the guard on in the child.
    Raises BrokerLeak when the caller points the child at the real post box."""
    new = {os.fsdecode(k): os.fsdecode(v) for k, v in dict(os.environ if env is None else env).items()}
    if "POSTBOX_NATS_URL" in new and names_real_broker(new["POSTBOX_NATS_URL"]):
        raise BrokerLeak(f"a test tried to start a process with POSTBOX_NATS_URL={new['POSTBOX_NATS_URL']!r}, the real post box")
    new[MARKER] = "1"
    new["PYTHONDONTWRITEBYTECODE"] = "1"   # the guard's own sitecustomize must not leave a __pycache__ in the tree that gets published
    extra = sorted(PROTECTED - {DEFAULT_PORT})
    if extra:
        new[PORTS_VAR] = ",".join(str(p) for p in extra)
    old = [p for p in new.get("PYTHONPATH", "").split(os.pathsep) if p and p != SITE_DIR]
    new["PYTHONPATH"] = os.pathsep.join([SITE_DIR, *old])
    return new


PY_VALUE_OPTIONS = {"--check-hash-based-pycs"}   # long options of the python command line that take a value as the NEXT word (-W and -X do too)
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish"}
# Programs (other than Python itself, a shell running a vetted script file, and env without tampering) a guarded process may start.
# Anything else - nc, curl, perl, osascript, a compiled client - could talk to the post box without ever loading this guard.
ALLOWED_TOOLS = {"nats-server", "plutil", "xcode-select", "git", "ps", "lsof"}   # ps and lsof only list processes (postbox/lib.py asks them about a pid)
GUARD_VARS = {MARKER, PORTS_VAR, "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", REEXEC_VAR}   # env tampering with these drops the guard in the child
_SHELL_META = re.compile(r"[;&|<>()$`\\\n*?\[\]{}~!#]")
_SCRIPT_RED_FLAGS = re.compile(
    r"\b(unset|eval|source|env|exec\s+-[a-z]*[ca]|nc|ncat|netcat|curl|wget|telnet|socat|ssh|scp|openssl|osascript|perl|ruby|node|php|lua|nats|"
    r"4222|(?-i:PYTHONPATH|PBGUARD\w*))\b|/dev/(tcp|udp)|python[0-9.]*\s+-[A-Za-z]*[ISE]|^\s*\.\s", re.I | re.M)


def _python_skips_guard(argv):
    """True when `argv` (the words after the program name) starts Python in a mode that ignores PYTHONPATH or site (-I, -S, -E): the child
    would run without the guard. Reads the real command line grammar: -c/-m end the options, -W/-X and --check-hash-based-pycs take a value
    (so a value is never mistaken for a flag, and a flag after a value is still seen), flags may be combined (-BI)."""
    skip_next = False
    for arg in argv:
        arg = os.fsdecode(arg)
        if skip_next:
            skip_next = False
            continue
        if arg == "--" or arg == "-" or not arg.startswith("-"):
            return False                                  # script name, stdin or end of options: nothing after it is a Python option
        if arg.startswith("--"):
            skip_next = arg in PY_VALUE_OPTIONS            # `--opt=value` carries its value; bare `--opt` takes the next word
            continue
        for pos, ch in enumerate(arg[1:], start=1):
            if ch in "ISE":
                return True
            if ch in "cm":
                return False                              # the rest of this word, or the next word, is the code or module: options are over
            if ch in "WX":
                skip_next = pos == len(arg) - 1            # `-Wvalue` carries its value; bare `-W` takes the next word
                break
    return False


def _real(path):
    return os.path.realpath(path) if path else path


def _resolve(program, cwd=None):
    """The file `program` would start (PATH lookup when it has no slash), or None when there is no such file."""
    program = os.fsdecode(program)
    if os.sep not in program:
        found = shutil.which(program)
        return _real(found) if found else None
    if not os.path.isabs(program) and cwd:
        program = os.path.join(os.fsdecode(cwd), program)
    return _real(program) if os.path.exists(program) else None


def _is_python(path, argv0):
    mine = {_real(sys.executable), _real(getattr(sys, "_base_executable", sys.executable))}
    names = {os.path.basename(os.fsdecode(argv0)), os.path.basename(path or "")}
    return (path in mine) or any(re.fullmatch(r"python[0-9.]*", n, re.I) for n in names)


def _shebang(path):
    """The interpreter command a script's first line names (['/usr/bin/env', 'python3'] and so on), or None for anything that is not a script."""
    try:
        with open(path, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    words = head[2:].split(b"\n")[0].decode("utf-8", "replace").split()
    return words or None


def _refuse(why, argv):
    raise BrokerLeak(f"a test tried to start a process the guard cannot follow: {why} ({[os.fsdecode(a) for a in argv][:5]!r})")


def _vet_env(argv):
    """`env [NAME=value ...] command ...`: fine unless it clears or rewrites the variables that carry the guard into the child."""
    i = 1
    while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i]):
        word = argv[i]
        if word.startswith("-"):
            if word in ("-u", "--unset") or word.startswith("--unset=") or word.startswith("-u"):
                _refuse("env -u would remove a variable the guard needs", argv)
            _refuse("env with options (-i, -S, -P, ...) can clear the guard's variables", argv)
        if word.split("=", 1)[0] in GUARD_VARS:
            _refuse("env would overwrite a variable the guard needs", argv)
        i += 1
    if i >= len(argv):
        return
    _vet(argv[i:])


def _vet_command_string(text, argv_for_message):
    """A `sh -c` string or a shell=True command: only a plain command (no operators, expansions, redirections or assignments) is followed."""
    if _SHELL_META.search(text):
        _refuse("a shell command line with operators or expansions cannot be followed", argv_for_message)
    try:
        words = shlex.split(text)
    except ValueError:
        _refuse("an unparseable shell command line", argv_for_message)
    if not words or "=" in words[0]:
        _refuse("a shell command line that starts with an assignment cannot be followed", argv_for_message)
    _vet(words)


def _vet_shell(path, argv):
    """A shell may run (a) one plain command given with -c, or (b) a script FILE whose text has none of the constructs that clear the guard
    or talk to a network on their own. A shell reading stdin, or running anything else, is refused."""
    args = [os.fsdecode(a) for a in argv[1:]]
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        word = args[i]
        if word == "--":
            i += 1
            break
        if word.startswith("--") or re.search(r"[sirR]", word[1:]) and "c" not in word[1:]:
            _refuse("a shell option that reads stdin, an rc file or a profile", argv)
        if "c" in word[1:]:
            if i + 1 >= len(args):
                _refuse("a shell -c without a command", argv)
            return _vet_command_string(args[i + 1], argv)
        i += 1
    if i >= len(args):
        _refuse("a shell with no script (it would read stdin)", argv)
    script = _resolve(args[i])
    if script is None or not os.path.isfile(script):
        return                                            # no such file: the shell fails by itself, nothing runs
    with open(script, "rb") as f:
        text = f.read(65536).decode("utf-8", "replace")
    hit = _SCRIPT_RED_FLAGS.search(text)
    if hit:
        _refuse(f"the shell script {script} contains {hit.group(0).strip()!r}, which can drop the guard or reach a broker on its own", argv)


def _vet(argv, program=None, cwd=None, depth=0):
    """Raise BrokerLeak unless starting `argv` (optionally with `program` as the file to run) keeps the guard on in the child."""
    argv = [os.fsdecode(a) for a in argv]
    if not argv or depth > 6 or _applying_kernel_layer:
        return
    prog = os.fsdecode(program) if program is not None else argv[0]
    path = _resolve(prog, cwd)
    if path is None:
        return                                            # no such program: the start fails by itself, nothing runs
    base = os.path.basename(path)
    if _is_python(path, argv[0]):
        if _python_skips_guard(argv[1:]):
            _refuse("Python started with -I, -S or -E skips the guard", argv)
        return
    script = _shebang(path)
    if script is not None:
        return _vet([*script, path, *argv[1:]], depth=depth + 1)
    if base == "env" or os.path.basename(argv[0]) == "env":
        return _vet_env(argv)
    if base in SHELLS:
        return _vet_shell(path, argv)
    if base in ALLOWED_TOOLS:
        return
    _refuse(f"{base!r} is not a program the tests may start (only Python, {', '.join(sorted(ALLOWED_TOOLS))}, a vetted shell script, env without tampering)", argv)


def _refuse_unguarded(argv, program=None, cwd=None):
    _vet(argv, program, cwd)


def _argv_of(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(a) for a in args]


def _patch_sockets():
    if getattr(socket.socket.connect, "_pbguard", False):
        return
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def watched(address):
        if isinstance(address, tuple) and len(address) >= 2 and address[1] in PROTECTED and is_local_host(address[0], resolve=True):
            raise BrokerLeak(f"a test tried to connect to {address[0]}:{address[1]}, the real post box")

    def connect(self, address):
        watched(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        watched(address)
        return real_connect_ex(self, address)

    connect._pbguard = connect_ex._pbguard = True
    connect_ex._pbguard_real = real_connect_ex
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex


def _reassert_environ():
    """Put the guard's variables back into os.environ (a test may have deleted them) before anything that inherits it starts."""
    os.environ.update({k: v for k, v in child_env(os.environ).items() if k in GUARD_VARS})


def _wrap_os(name, path_index, argv_index, env_index=None):
    real = getattr(os, name, None)
    if real is None or getattr(real, "_pbguard", False):
        return

    @functools.wraps(real)
    def wrapper(*args, **kw):
        args = list(args)
        path = args[path_index] if len(args) > path_index else kw.get("path", kw.get("file"))
        argv = args[argv_index] if len(args) > argv_index else kw.get("argv", kw.get("args"))
        if isinstance(argv, (list, tuple)):
            _vet(argv, path if isinstance(path, (str, bytes, os.PathLike)) else None)
        if env_index is None:
            _reassert_environ()
        elif len(args) > env_index:
            args[env_index] = child_env(args[env_index])
        else:
            kw["env"] = child_env(kw.get("env"))
        return real(*args, **kw)

    wrapper._pbguard = True
    setattr(os, name, wrapper)


def _wrap_system():
    real = os.system
    if getattr(real, "_pbguard", False):
        return

    @functools.wraps(real)
    def system(command):
        _vet_command_string(os.fsdecode(command), [command])
        _reassert_environ()
        return real(command)

    system._pbguard = True
    os.system = system


def _wrap_fork_exec():
    """subprocess and multiprocessing start every child through this one C function; wrapping it covers a caller that skips Popen."""
    try:
        import _posixsubprocess
    except ImportError:
        return
    real = getattr(_posixsubprocess, "fork_exec", None)
    if real is None or getattr(real, "_pbguard", False):
        return

    @functools.wraps(real)
    def fork_exec(*a):
        a = list(a)
        if len(a) > 5 and isinstance(a[0], (list, tuple)) and a[0]:
            candidates = [c for c in (a[1] or ()) if _resolve(c)]
            _vet(a[0], candidates[0] if candidates else None)
            if a[5] is not None:
                env = dict(os.fsdecode(item).split("=", 1) for item in a[5])
                a[5] = [os.fsencode(f"{k}={v}") for k, v in child_env(env).items()]
        return real(*a)

    fork_exec._pbguard = True
    _posixsubprocess.fork_exec = fork_exec
    if getattr(subprocess, "_fork_exec", None) is real:
        subprocess._fork_exec = fork_exec


def _patch_processes():
    if getattr(subprocess.Popen.__init__, "_pbguard", False):
        return
    real_init = subprocess.Popen.__init__

    @functools.wraps(real_init)
    def init(self, args, *rest, **kw):
        shell = kw.get("shell", rest[7] if len(rest) > 7 else False)
        executable = kw.get("executable", rest[1] if len(rest) > 1 else None)
        cwd = kw.get("cwd", rest[8] if len(rest) > 8 else None)
        words = _argv_of(args)
        if shell:
            if executable is not None and os.path.basename(os.fsdecode(executable)) not in SHELLS:
                _refuse("shell=True with an executable that is not a shell", words)
            _vet_command_string(words[0] if words else "", words)
        else:
            _vet(words, executable, cwd)
        if len(rest) > 9:
            rest = (*rest[:9], child_env(rest[9]), *rest[10:])
        else:
            kw["env"] = child_env(kw.get("env"))
        real_init(self, args, *rest, **kw)

    init._pbguard = True
    subprocess.Popen.__init__ = init
    _wrap_fork_exec()
    _wrap_system()
    for name in ("execve", "posix_spawn", "posix_spawnp"):
        _wrap_os(name, 0, 1, 2)
    _wrap_os("execv", 0, 1)
    for name in ("spawnve", "spawnvpe"):
        _wrap_os(name, 1, 2, 3)
    for name in ("spawnv", "spawnvp"):
        _wrap_os(name, 1, 2)


def kernel_enforced():
    """True only when the operating system is refusing connections in THIS process: a connection to CANARY_PORT on this Mac, a port nothing
    listens on, gets EPERM from the kernel under kernel_profile and ECONNREFUSED otherwise. Decided by trying, never by an environment
    variable or a marker a caller could set. Nothing is sent to a protected port."""
    if sys.platform != "darwin":
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(5)
        return _REAL_CONNECT_EX(s, ("127.0.0.1", CANARY_PORT)) == errno.EPERM
    except OSError:
        return False
    finally:
        s.close()


def _fail_closed(why):
    sys.stderr.write(f"tests refuse to run: the operating-system layer of tests/_guard.py could not be established ({why}). Without it a native "
                     "program or a shell script started by a test could reach a real post box. Run the tests from a normal Terminal on macOS, "
                     "outside any other sandbox (a sandbox cannot be nested).\n")
    sys.stderr.flush()
    sys.exit(3)


def _enter_kernel_layer():
    """The layer the Python patches cannot be: the operating system itself refuses every connection to a protected port on this Mac, from
    this process and from every process it ever starts (a profile applied with sandbox-exec is inherited and cannot be removed), whatever
    the program is written in, whatever its environment or command line says, and whether it was started through Python or not (ctypes,
    a shell script, a compiled client). Required, and fail closed. A process that is already under the profile (kernel_enforced, decided by
    trying, not by any variable) carries on. Otherwise it re-starts this very command under sandbox-exec. If that is impossible (not macOS,
    no sandbox-exec, already inside another sandbox, which forbids a second one) or if the re-started process is still not under the
    profile, the process exits with code 3. REEXEC_VAR marks a process that was re-started: it ends a loop, and a caller who sets it only
    makes the guard refuse sooner."""
    global _applying_kernel_layer
    if kernel_enforced():
        os.environ.pop(REEXEC_VAR, None)
        return
    if os.environ.get(REEXEC_VAR):
        _fail_closed("this process was re-started under sandbox-exec and the kernel still allows a connection to the canary port, or the marker "
                     f"{REEXEC_VAR} was set by hand")
    if sys.platform != "darwin":
        _fail_closed(f"this is not macOS (sys.platform={sys.platform!r})")
    if not os.path.exists(SANDBOX_EXEC):
        _fail_closed(f"{SANDBOX_EXEC} does not exist")
    profile = kernel_profile(PROTECTED)
    _applying_kernel_layer = True
    try:
        try:
            probe = subprocess.run([SANDBOX_EXEC, "-p", profile, "/usr/bin/true"], capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as exc:
            _fail_closed(f"starting sandbox-exec failed: {type(exc).__name__}: {exc}")
        if probe.returncode != 0:
            _fail_closed(f"sandbox-exec exited {probe.returncode}: {probe.stderr.decode('utf-8', 'replace').strip()[-200:]!r}; this process is "
                         "probably already inside a sandbox")
        os.environ[REEXEC_VAR] = "1"
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(SANDBOX_EXEC, [SANDBOX_EXEC, "-p", profile, sys.executable, *getattr(sys, "orig_argv", [sys.executable, *sys.argv])[1:]])
    finally:
        _applying_kernel_layer = False


def install(child=False):
    """`child` is True when tests/_guard_site/sitecustomize.py loads this file into a process some test started: that process gets the
    connection guard and the refusal of the default address, but not the spare-port and throwaway-folder defaults (a test may have set
    HOME and the support folder up on purpose, and the installer test checks where files land)."""
    PROTECTED.clear()
    PROTECTED.update(_protected_from_env())
    if not child:                                     # a child gets the same set through PBGUARD_PORTS (child_env)
        found, problems = installed_ports()
        PROTECTED.update(found)
        for problem in problems:
            sys.stderr.write(f"tests/_guard.py warning: a launchd job file that may name a post box port was not usable, so that port may be unprotected: {problem}\n")
    if "POSTBOX_NATS_URL" in os.environ:
        refuse_default(os.environ["POSTBOX_NATS_URL"])
    elif not child:
        os.environ["POSTBOX_NATS_URL"] = f"nats://127.0.0.1:{spare_port()}"   # nothing listens there
    if not child and "POSTBOX_SUPPORT_DIR" not in os.environ:
        os.environ["POSTBOX_SUPPORT_DIR"] = tempfile.mkdtemp(prefix="postbox-test-support-")
    if not child:
        _enter_kernel_layer()   # re-starts this command under sandbox-exec, or exits with code 3; nothing below runs in the old process then
    _reassert_environ()         # children that inherit os.environ
    _patch_sockets()
    _patch_processes()


install(child=(__name__ == "_guard_boot"))   # tests/_guard_site/sitecustomize.py loads this file under that name in every guarded child
