"""Post box shared code: addresses, finding live sessions, and the per-tool delivery doors.

Plain software only. No AI is involved anywhere in here.
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
import stat
import subprocess
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_PORT = 4222
NATS_URL = os.environ.get("POSTBOX_NATS_URL", f"nats://127.0.0.1:{DEFAULT_PORT}")
STREAM = "POST"
SOCK_DIR = os.environ.get("POSTBOX_CLAUDE_SOCK_DIR", "/tmp/cc-socks")
MAX_AGE_S = int(os.environ.get("POSTBOX_MAX_AGE_S", str(7 * 24 * 3600)))   # queued mail (and an unread Grok inbox message) lives this long: a Mac that sleeps for a weekend still gets it
STALE_AFTER_S = int(os.environ.get("POSTBOX_STALE_AFTER_S", "1800"))        # mail that waited longer than this reaches the receiver with a "do not act yet, ask the sender" banner
DUPE_WINDOW_S = 120
PAIR_LIMIT = int(os.environ.get("POSTBOX_PAIR_LIMIT", "30"))
PAIR_WINDOW_S = int(os.environ.get("POSTBOX_PAIR_WINDOW_S", "600"))
RECIPIENT_LIMIT = int(os.environ.get("POSTBOX_RECIPIENT_LIMIT", "120"))   # all senders together, per recipient, per window: a sender cannot dodge the pair limit by changing its name
MAX_TEXT_BYTES = 32_000


# ---------------------------------------------------------------- telemetry (metadata only; see telemetry.py and POST-TELEMETRY.md)
class _NoTelemetry:
    """Stands in when telemetry.py cannot be loaded: a broken telemetry file must never stop mail."""
    _DEFAULTS = {"emit": False, "elapsed_ms": None, "text_facts": {}, "harness_hint": {}, "connect_error_class": "other",
                 "error_class": "other", "error_class_of_text": "message", "classify_refusal": "other", "limit_of": None,
                 "status_for": "error", "caller": None, "stats": {}, "machine": "unknown", "identity": "unknown",
                 "enabled": False}

    def __getattr__(self, name):
        default = self._DEFAULTS.get(name)
        return lambda *args, **kwargs: (dict(default) if isinstance(default, dict) else default)


def _load_telemetry():
    try:
        try:
            from . import telemetry as module   # imported as part of the postbox package (a script that imports the folder as a package)
        except ImportError:
            import telemetry as module          # imported as a top-level module (the scripts put their own folder on sys.path)
        return module
    except Exception:  # noqa: BLE001
        return _NoTelemetry()


telemetry = _load_telemetry()


def _load_doors():
    try:
        from . import doors as module   # imported as part of the postbox package
    except ImportError:
        import doors as module          # imported as a top-level module (the scripts put their own folder on sys.path)
    return module


doors = _load_doors()   # which delivery doors are switched on (postbox/doors.py)


def norm(name) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s or "unknown"


# ---------------------------------------------------------------- this Mac, and the other Macs it is linked to
def support_dir() -> Path:
    """Per-Mac private files: this Mac's login key (machine.seed) and the list of linked Macs (link.json)."""
    return Path(os.environ.get("POSTBOX_SUPPORT_DIR") or Path.home() / "Library" / "Application Support" / "postbox")


def project_aliases() -> dict:
    """aliases.json in the support folder: {"project-g": "project-g-full"} makes both names ONE project (mail, sessions, inboxes, wake entries).
    The right-hand name is the one shown. Missing or unreadable means no aliases. Read on every call (a tiny file), so an edit applies at once."""
    try:
        data = json.loads((support_dir() / "aliases.json").read_text())
    except (OSError, ValueError):
        return {}
    return {norm(k): norm(v) for k, v in data.items() if isinstance(data, dict) and isinstance(v, str) and k} if isinstance(data, dict) else {}


def canon_project(name) -> str:
    """The one name a project goes by after aliases (norm first, then follow the alias table; a loop in the table cannot hang it)."""
    aliases, n, seen = project_aliases(), norm(name), set()
    while n in aliases and n not in seen:
        seen.add(n)
        n = aliases[n]
    return n


def same_project(a, b) -> bool:
    return canon_project(a) == canon_project(b)


def load_link() -> dict:
    """link.json: this Mac's name and the other Macs it can reach. Missing or unreadable means not linked (empty)."""
    try:
        data = json.loads((support_dir() / "link.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def my_machine() -> str:
    """This Mac's short name: POSTBOX_MACHINE, else what `install.py --link` recorded, else the host name."""
    return norm(os.environ.get("POSTBOX_MACHINE") or load_link().get("machine") or socket.gethostname().split(".")[0])


SEED_RE = re.compile(r"^SU[A-Z2-7]{56}$")
INBOX_RE = re.compile(r"^_INBOX(\.[A-Za-z0-9_-]+){1,4}$")   # the only subjects the relay ever answers to


def machine_seed() -> Optional[str]:
    """This Mac's private login key (an nkey seed), or None on an install from before logins existed.
    A key file that exists but is not a key is an error in plain words, never a silent "no login"."""
    try:
        text = (support_dir() / "machine.seed").read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    if not SEED_RE.match(text):
        raise MailboxError(f"this Mac's login key file is damaged ({support_dir() / 'machine.seed'}). Move it away and run "
                           "python3 <postbox folder>/install.py to make a new one (other linked Macs then need the new public key)", setup=True)
    return text


def public_key_of(seed) -> str:
    """The PUBLIC key of a seed. Needs the nkeys library, which the post box's own python environment has."""
    try:
        import nkeys
        return nkeys.from_seed(seed.encode()).public_key.decode()
    except Exception as exc:  # noqa: BLE001 - say what to do, never print key material
        raise MailboxError(f"cannot read this Mac's login key ({type(exc).__name__}); run python3 <postbox folder>/install.py", setup=True)


def peer_inbox_prefix(public_key) -> str:
    """The reply-inbox name a linked Mac must use, derived from its PUBLIC key. The mailbox lets that key listen only under this name,
    so it cannot hear mail deliveries or anyone else's replies (both travel on other _INBOX names). Both ends compute it: the
    receiving Mac from the key it trusts, the sending Mac from its own key."""
    return "_INBOX.p" + str(public_key)[8:24]


def clean_address(text) -> str:
    """A sender address made safe to put in a shell command: agent@project[@machine], each part lower-case letters, digits, hyphens.
    Mail from another Mac names its own sender, so the receiver never trusts the raw text: anything unusable becomes 'unknown'."""
    parts = str(text or "").strip().split("@")
    if not 1 <= len(parts) <= 3 or any(not p.strip() for p in parts):
        return "unknown"
    return "@".join(norm(p) for p in parts)


@dataclass(frozen=True)
class Target:
    """One mailbox to talk to: where it is, the key to log in with (None = no login) and a name for messages."""
    url: str
    seed: Optional[str] = field(repr=False)   # the private key: never shown when a Target is printed or logged
    label: str
    inbox_prefix: Optional[str] = None        # set for another Mac's mailbox: the only reply-inbox name that key may listen on

    def auth(self) -> dict:
        opts = {"nkeys_seed_str": self.seed} if self.seed else {}
        if self.inbox_prefix:
            opts["inbox_prefix"] = self.inbox_prefix
        return opts


class MailboxError(Exception):
    """A mailbox could not be used. `refused`: it answered but rejected our key. `setup`: the message is already a complete
    plain sentence about what to fix (unknown Mac, no key yet). Neither: it was unreachable and the message is the raw reason.
    `transient` (only with setup): the problem fixes itself with time (a linked Mac's name that does not resolve right now because Tailscale is
    off or that Mac left the tailnet): it counts as unreachable, so the sender stores the mail and forwards it later instead of failing."""

    def __init__(self, message, refused=False, setup=False, transient=False):
        super().__init__(message)
        self.refused = refused
        self.setup = setup
        self.transient = transient


def local_target() -> Target:
    return Target(NATS_URL, machine_seed(), "this Mac")


def _ipv4(host, port, timeout=4.0):
    """IPv4 address of `host`. IPv4 first, always: on this LAN the first answer can be an unusable fe80:: address.
    The lookup runs in a daemon thread so a broken resolver can neither freeze the caller nor block its exit."""
    import threading
    box = {}

    def work():
        try:
            box["ip"] = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except OSError as exc:
            box["err"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if "ip" in box:
        return box["ip"]
    raise box.get("err") or OSError(f"the name lookup took over {timeout:g} s")


def peer_target(name) -> Target:
    """The mailbox of another linked Mac, found by name. Env POSTBOX_PEER_<NAME>=host[:port] overrides link.json."""
    name = norm(name)
    peers = load_link().get("peers") or {}
    entry = peers.get(name) or {}
    host, _, port_text = (os.environ.get("POSTBOX_PEER_" + name.upper().replace("-", "_")) or entry.get("host") or "").partition(":")
    if not host:
        linked = ", ".join(sorted(peers)) or "none"
        raise MailboxError(f"no other Mac called '{name}' is linked to this one (linked: {linked}). Link it: python3 <postbox folder>/install.py --link --peer {name}", setup=True)
    seed = machine_seed()
    if not seed:
        raise MailboxError(f"this Mac has no login key yet, so it cannot use {name}'s post box. Run: python3 <postbox folder>/install.py --link --peer {name}", setup=True)
    port = int(port_text or entry.get("port") or DEFAULT_PORT)
    try:
        ip = _ipv4(host, port)
    except OSError as exc:
        raise MailboxError(f"cannot find {name} on the network ({host}: {exc}). Is Tailscale running on both Macs? "
                           f"If {name}'s Tailscale name changed, run python3 <postbox folder>/install.py --link --peer {name} to look it up again.", setup=True, transient=True)
    return Target(f"nats://{ip}:{port}", seed, name, inbox_prefix=peer_inbox_prefix(public_key_of(seed)))


async def connect_fast(target: Target, timeout=3):
    """Open a mailbox connection that fails at once when the mailbox is down or rejects our key (raises MailboxError).

    Measured with nats-py 2.x against a closed port: max_reconnect_attempts=0 and allow_reconnect=False both retry the FIRST
    connection forever (a caller froze); max_reconnect_attempts=1 with a short wait fails in about 0.3 s. error_cb keeps
    nats-py from printing a traceback, and it is where a rejected login shows up ("authorization violation")."""
    import nats
    seen = []

    async def on_error(err=None):
        seen.append(repr(err))

    try:
        return await nats.connect(target.url, connect_timeout=timeout, max_reconnect_attempts=1, reconnect_time_wait=0.2,
                                  error_cb=on_error, **target.auth())
    except Exception as exc:  # noqa: BLE001
        detail = " ".join(seen + [repr(exc)])
        if "InvalidSeed" in detail or "ErrInvalid" in detail:
            raise MailboxError("this Mac's login key file is damaged: run python3 <postbox folder>/install.py", setup=True) from exc
        raise MailboxError(detail, refused="uthorization" in detail or "uthentication" in detail) from exc


def parse_target(text, default_project, mine=None):
    """'claude' -> ('claude', default_project, None); 'codex@project-b' -> ('codex', 'project-b', None);
    'codex@project-b@mac-b' -> ('codex', 'project-b', 'mac-b'); 'claude@@mac-b' -> my own project, on mac-b.
    The third part is None when it names this Mac (there is no other Mac to go to)."""
    text = (text or "").strip()
    parts = [p.strip() for p in text.split("@")]
    bad = ValueError(f"bad address {text!r}: use agent, agent@project or agent@project@machine, for example claude, codex@project-b or claude@dotclaude@mac-b")
    if len(parts) > 3 or not parts[0] or "#" in text:
        raise bad
    agent = norm(parts[0])
    if agent == "unknown":
        raise bad
    project = parts[1] if len(parts) > 1 and parts[1] else default_project
    machine = None
    if len(parts) == 3:
        if not parts[2]:
            raise bad
        machine = norm(parts[2])
        if machine == (mine or my_machine()):
            machine = None
    return agent, project, machine


def valid_session(text) -> Optional[str]:
    """The cleaned session name for `text` (the part after '#' in agent@project#name), or None when it is not usable as one: empty, an address
    (@ or #), control characters, nothing left after cleaning, or longer than NAME_MAX. Cleaned the same way as project names (lower case
    letters, digits, hyphens), so 'PROJECT-A' and 'project-a' are the same session and no value can carry a shell character."""
    raw = str(text if text is not None else "").strip()
    if not raw or len(raw) > 4 * NAME_MAX or "@" in raw or "#" in raw or _CONTROL_RE.search(raw):
        return None
    cleaned = norm(raw)
    return None if cleaned == "unknown" or len(cleaned) > NAME_MAX else cleaned


def parse_destination(text, default_project, mine=None):
    """parse_target plus an optional session: 'claude@project-a#algorithm' -> ('claude', 'project-a', None, 'algorithm');
    'claude@project-a@mac-b#algorithm' -> ('claude', 'project-a', 'mac-b', 'algorithm'). The '#name' always comes LAST, after the machine.
    No '#name' gives session None. A '#' with nothing usable after it (or a second '#') is an error, never 'any session'."""
    text = (text or "").strip()
    head, sep, name = text.partition("#")
    session = None
    if sep:
        session = valid_session(name)
        if session is None:
            raise ValueError(f"bad session name in {text!r}: put the session's name after one #, last, using letters, digits and hyphens "
                             f"(up to {NAME_MAX}), for example claude@project-a#algorithm or claude@project-a@mac-b#algorithm; `post who` lists the names")
    agent, project, machine = parse_target(head, default_project, mine)
    return agent, project, machine, session


def parse_address(text, default_project):
    """Same as parse_target for an address on this Mac: 'claude' -> ('claude', default_project); 'codex@project-b' -> ('codex', 'project-b')."""
    agent, project, machine = parse_target(text, default_project)
    if machine:
        raise ValueError(f"{text!r} names another Mac ({machine}); this call only handles addresses on this Mac")
    return agent, project


@dataclass(frozen=True)
class Session:
    agent: str
    pid: int
    cwd: str
    project: str
    target: str  # Claude: inbox socket path. Codex: thread id.
    ready: bool = True  # False: a Codex screen that has not started its own conversation yet (no thread to hand mail to)
    source: str = "folder"  # where `project` came from: "declaration" (post join), "marker" (a .postbox-project file) or "folder"
    where: str = ""         # the marker file's path when source is "marker"
    note: str = ""          # one line when a marker or declaration was found but could not be used (shown by post who / post whoami)
    name: str = ""          # the session's own name, cleaned (Claude: /rename or the automatic one; Codex: the thread's name), "" when it has none
    sid: str = ""           # 6 hex characters of the session's own id: a second, always-unique-enough way to name it

    @property
    def sock(self) -> str:
        return self.target

    @property
    def handle(self) -> str:
        """What a message names this session by after '#': its name, else its short id, else (only when discovery found neither) p<pid>."""
        return self.name or self.sid or f"p{self.pid}"

    def matches(self, want) -> bool:
        return bool(want) and want in {self.name, self.sid, self.handle} - {""}

    @property
    def label(self) -> str:
        tag = f"#{self.name or self.sid}" if (self.name or self.sid) else ""
        return f"{self.agent}@{self.project}{tag} (pid {self.pid})"


def _run(cmd, timeout=5) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def cwd_of(pid):
    for line in _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


# ---------------------------------------------------------------- which project does a session belong to?
# One resolver, used everywhere a session's project is derived. Precedence: (a) a live declaration (`post join`) made by that exact
# process, (b) the nearest `.postbox-project` marker file in the session's folder or an ancestor (the walk stops at $HOME), (c) the folder's
# name. The answer always goes through norm(). This is sender discovery, not authentication: anyone who can run `post` here can already
# claim a name with --as.
PROJECT_MARKER = ".postbox-project"
MARKER_MAX_BYTES = 1024
REG_MAX_BYTES = 4096
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ProjectInfo:
    project: str
    source: str = "folder"   # declaration, marker or folder
    where: str = ""          # the marker file's path
    note: str = ""


def valid_project(text) -> Optional[str]:
    """The cleaned project name for `text`, or None when it is not usable as one (empty, an address with @, control characters,
    the word unknown, longer than NAME_MAX after cleaning)."""
    raw = str(text if text is not None else "").strip()
    if not raw or len(raw) > 4 * NAME_MAX or "@" in raw or _CONTROL_RE.search(raw):
        return None
    cleaned = norm(raw)
    return None if cleaned == "unknown" or len(cleaned) > NAME_MAX else cleaned


def reg_dir() -> Path:
    """Where `post join` keeps its small per-process registrations (private to this user)."""
    return Path(os.environ.get("POSTBOX_REG_DIR") or support_dir() / "projects")


def process_start(pid) -> Optional[str]:
    """When the process started, as `ps` prints it (`Fri Oct  2 14:55:19 2026`, whitespace squeezed), else None. A reused process number
    starts at a different time, so a registration made for the old process never matches the new one."""
    return " ".join(_run(["ps", "-p", str(pid), "-o", "lstart="]).split()) or None


def _reg_path(pid) -> Path:
    return reg_dir() / f"{int(pid)}.json"


def _read_reg(path: Path):
    """The registration dict in `path`, or None when it is missing, too big or not a registration."""
    try:
        if path.stat().st_size > REG_MAX_BYTES:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _reg_live(data, agent, pid) -> bool:
    """True when `data` was made by THIS live process: same agent, same process number, and the process started when it said."""
    if data.get("agent") != str(agent) or data.get("pid") != pid or isinstance(pid, bool) or not isinstance(data.get("start"), str):
        return False
    return pid_alive(pid) and process_start(pid) == data["start"]


def declared_project(agent, pid):
    """(project or None, note) from a live declaration made by exactly this process. Dead or mismatching ones are ignored without a word
    (the relay's heartbeat sweeps them); one that is live but carries an unusable name is ignored with a note."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None, ""
    data = _read_reg(_reg_path(pid))
    if data is None or not _reg_live(data, agent, pid):
        return None, ""
    project = valid_project(data.get("project"))
    if project is None:
        return None, f"ignored the declaration for process {pid}: its project name is not usable"
    return project, ""


def find_marker(cwd):
    """(project or None, marker path or "", note) for the nearest usable `.postbox-project` in cwd or an ancestor. The walk stops at $HOME
    (the home folder itself is not searched: one file there would rename every session below it). An unusable marker is skipped with a note."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None, "", ""
    try:   # the walk is plain text on the path as given (the process's own cwd is already a real path): one stat per folder level
        home = {os.path.normpath(str(Path.home())), os.path.realpath(Path.home())}
    except (OSError, RuntimeError):
        return None, "", ""
    folder, notes = os.path.normpath(cwd), []
    for _ in range(64):
        if folder in home:
            break
        path = os.path.join(folder, PROJECT_MARKER)
        try:
            regular = stat.S_ISREG(os.lstat(path).st_mode)   # a marker that is a symlink or a folder is not a marker
        except OSError:
            regular = False
        if regular:
            value = None
            try:
                with open(path, "rb") as fh:
                    text = fh.read(MARKER_MAX_BYTES).decode("utf-8", "replace")
                for line in text.splitlines():
                    line = line.split("#", 1)[0].strip()
                    if line:
                        value = line
                        break
            except OSError:
                pass
            project = valid_project(value)
            if project:
                return project, path, "; ".join(notes)
            notes.append(f"ignored {path}: its first line is not a usable project name")
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None, "", "; ".join(notes)


def project_info(agent, pid, cwd) -> ProjectInfo:
    """The project a session belongs to, and where that came from. `pid` is None for a session with no process number (--as)."""
    notes = []
    project, note = declared_project(agent, pid)
    if note:
        notes.append(note)
    if project:
        return ProjectInfo(project, "declaration", "", "; ".join(notes))
    project, where, note = find_marker(cwd)
    if note:
        notes.append(note)
    if project:
        return ProjectInfo(project, "marker", where, "; ".join(notes))
    cwd = cwd or ""
    return ProjectInfo(norm(os.path.basename(cwd.rstrip("/")) or cwd), "folder", "", "; ".join(notes))


def project_of(agent, pid, cwd) -> str:
    """Just the project name (see project_info)."""
    return project_info(agent, pid, cwd).project


def declare(agent, pid, project, now=None) -> Path:
    """Register process `pid` (of `agent`) as a member of `project` only. Returns the file written (mode 600, in a mode 700 folder).
    Raises ValueError for an unusable name or a process that is not running."""
    cleaned = valid_project(project)
    if cleaned is None:
        raise ValueError(f"{str(project)[:40]!r} is not a project name: use letters, digits and hyphens, up to {NAME_MAX}, for example myproject")
    start = process_start(pid)
    if start is None:
        raise ValueError(f"cannot read when process {pid} started, so it cannot be registered")
    folder = reg_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    final = _reg_path(pid)
    tmp = folder / f".tmp-{uuid.uuid4().hex[:12]}"
    entry = {"agent": str(agent), "pid": int(pid), "start": start, "project": cleaned, "declared_at": int(time.time() if now is None else now)}
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(entry))
    os.replace(tmp, final)
    return final


def undeclare(pid) -> bool:
    """Remove the declaration of process `pid`. True when there was one."""
    try:
        _reg_path(pid).unlink()
        return True
    except OSError:
        return False


def reg_sweep(now=None) -> int:
    """Remove declarations whose process is gone, started at another time (a reused number) or whose file is not a registration
    (the relay runs this on its heartbeat). Returns how many files were removed. Never raises."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = sorted(os.listdir(reg_dir()))
    except OSError:
        return 0
    for name in names:
        path = reg_dir() / name
        if name.startswith(".tmp-"):
            try:
                if now - path.stat().st_mtime > 3600:
                    _unlink(path)
                    removed += 1
            except OSError:
                pass
            continue
        data = _read_reg(path)
        pid = data.get("pid") if data else None
        if data is None or not isinstance(pid, int) or isinstance(pid, bool) or name != f"{pid}.json" or not _reg_live(data, data.get("agent"), pid):
            _unlink(path)
            removed += 1
    return removed


# ---------------------------------------------------------------- the name of a session (per-session addressing)
# Several sessions can be open in one project, so a message can name one: agent@project#name. Where the name comes from:
#   Claude Code: it keeps one small file per live session, <config dir>/sessions/<pid>.json, with `name` (set by /rename, or made up
#                automatically, `nameSource` says which) and `sessionId`. Read on every look, so a /rename applies at once.
#   Codex:       its thread table (state_*.sqlite, the same file the box already reads) has a `name` column for the thread. It holds the
#                title Codex shows for the conversation; the table does not say whether a person chose it or Codex made it up, and a thread
#                can have none, then the short id is the name. Two threads may share a title: that is told as "ambiguous", never guessed.
#   Grok / grokbot-desktop: one inbox per project; they have no per-session door, so no name.
SESSION_FILE_MAX = 65536
HEX6_RE = re.compile(r"[0-9a-f]{6}")


def claude_sessions_dir() -> Path:
    """Where Claude Code keeps its per-session files: $POSTBOX_CLAUDE_SESSIONS_DIR, else $CLAUDE_CONFIG_DIR/sessions, else ~/.claude/sessions."""
    return Path(os.environ.get("POSTBOX_CLAUDE_SESSIONS_DIR") or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions")


def claude_session_record(pid):
    """Claude Code's own record of the live session with this process number (its <pid>.json), or None when there is none, it is damaged,
    it names another pid, or it was written by an earlier process that had the same number (the recorded start time differs)."""
    try:
        path = claude_sessions_dir() / f"{int(pid)}.json"
        if path.stat().st_size > SESSION_FILE_MAX:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("pid") != int(pid):
        return None
    began = data.get("startedAt")
    if isinstance(began, (int, float)) and not isinstance(began, bool) and began > 1e12:
        # the file's own times are UTC while `ps` prints local time, so compare as epoch seconds (a few minutes of slack)
        actual = process_start(pid)
        try:
            actual_s = time.mktime(time.strptime(actual, "%a %b %d %H:%M:%S %Y")) if actual else None
        except ValueError:
            actual_s = None
        if actual_s is not None and abs(actual_s - began / 1000) > 300:
            return None
    return data


def _short_id(text) -> str:
    """The last 6 hex characters of an id (a UUID with its hyphens removed), else ''."""
    tail = str(text or "").replace("-", "").lower()[-6:]
    return tail if HEX6_RE.fullmatch(tail) else ""


def codex_thread_name(thread_id) -> str:
    """The name Codex's thread table holds for this thread (read-only), cleaned; '' when it has none or cannot be read."""
    import sqlite3
    db = codex_state_db()
    if not db or not thread_id:
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute("select name from threads where id=?", (thread_id,)).fetchone()
        con.close()
    except Exception:
        return ""
    return (valid_session(row[0]) or "") if row and isinstance(row[0], str) else ""


def session_identity(agent, pid, target):
    """(name, short id) of a live session, '' for what is not known. Claude: from its session file. Codex: from its thread (target = thread id)."""
    try:
        if agent == "claude":
            rec = claude_session_record(pid)
            if rec:
                return valid_session(rec.get("name")) or "", _short_id(rec.get("sessionId"))
        elif agent == "codex" and doors.enabled("codex") and target and UUID_RE.match(str(target)):
            return codex_thread_name(target), _short_id(target)
    except Exception:  # noqa: BLE001 - a name is a convenience; discovery of the session itself must never depend on it
        pass
    return "", ""


def sender_session(me):
    """The name a sender goes by after '#' (its session name, else its short id), or None when it cannot be told or names no process
    (--as, POSTBOX_AS, Grok). `me` is (agent, project, pid) from my_identity()."""
    if not me or me[2] is None:
        return None
    agent, pid = me[0], me[2]
    if agent == "claude":
        name, sid = session_identity("claude", pid, None)
    elif agent == "codex" and doors.enabled("codex"):
        thread = os.environ.get("CODEX_THREAD_ID", "")
        if not UUID_RE.match(thread):
            screen = codex_ancestor()
            thread = screen.target if screen is not None and screen.ready else ""
        name, sid = session_identity("codex", pid, thread)
    else:
        return None
    return name or sid or None


def _session(agent, pid, cwd, target, ready=True) -> Session:
    info = project_info(agent, pid, cwd)
    name, sid = session_identity(agent, pid, target) if ready else ("", "")
    return Session(agent, pid, cwd, info.project, target, ready, info.source, info.where, info.note, name, sid)


def claude_sessions():
    """Live Claude Code sessions on this Mac, found from their inbox sockets."""
    found = []
    for path in sorted(glob.glob(os.path.join(SOCK_DIR, "*.sock"))):
        try:
            pid = int(os.path.basename(path)[:-5])
        except ValueError:
            continue
        if not pid_alive(pid) or "claude" not in _run(["ps", "-p", str(pid), "-o", "command="]):
            continue
        cwd = cwd_of(pid)
        if cwd:
            found.append(_session("claude", pid, cwd, path))
    return found


# How the last my_identity() / resolve_sender() call decided who is sending, for telemetry: method is one of grok_process, claude_env,
# codex_thread, codex_process, flag_as, postbox_as, or none; failed lists the detections that were tried and found nothing.
IDENTITY_TRACE = {"method": "none", "failed": []}
IDENTITY_PROJECT = {"info": None}   # the ProjectInfo of the session the last my_identity() found (where its project came from), or None


def _traced(method, found):
    """Remember which detection produced `found` (or that none did) and hand `found` back unchanged."""
    IDENTITY_TRACE["method"] = method if found else "none"
    return found


def _identity_project(agent, pid, cwd) -> str:
    """The project of the session found, by the one resolver (declaration, marker, folder); remembers where it came from."""
    info = project_info(agent, pid, cwd)
    IDENTITY_PROJECT["info"] = info
    return info.project


def my_identity():
    """(agent, project, pid) of the agent session running this command, or None."""
    IDENTITY_TRACE.update(method="none", failed=[])
    IDENTITY_PROJECT["info"] = None
    # A Grok shell command carries GROK_AGENT. A Grok screen started from inside a Claude or Codex terminal also inherits THEIR variables,
    # and the Grok process is the nearer ancestor, so with that hint Grok is looked for first. Without it, Grok is the last thing tried.
    grok_on, codex_on = doors.enabled("grok"), doors.enabled("codex")   # a switched-off door is never looked for
    grok_hint = grok_on and bool(os.environ.get("GROK_AGENT"))
    if grok_hint:
        found = grok_identity()
        if found:
            return _traced("grok_process", found)
        IDENTITY_TRACE["failed"].append("grok_process")
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    m = re.search(r"(\d+)\.sock$", sock)
    pid = int(m.group(1)) if m else (int(os.environ["CLAUDE_PID"]) if os.environ.get("CLAUDE_PID", "").isdigit() else None)
    if os.environ.get("CLAUDECODE") and pid:
        cwd = cwd_of(pid)
        if cwd:
            return _traced("claude_env", ("claude", _identity_project("claude", pid, cwd), pid))
        IDENTITY_TRACE["failed"].append("claude_env")
    if codex_on:
        if "CODEX_THREAD_ID" in os.environ:
            found = native_sender_identity(os.environ["CODEX_THREAD_ID"])
            if not found:
                IDENTITY_TRACE["failed"].append("codex_thread")
            return _traced("codex_thread", found)
        cs = codex_ancestor()
        if cs:
            IDENTITY_PROJECT["info"] = ProjectInfo(cs.project, cs.source, cs.where, cs.note)
            return _traced("codex_process", ("codex", cs.project, cs.pid))
    if grok_hint or not grok_on:
        return _traced("none", None)
    found = grok_identity()
    if not found:
        IDENTITY_TRACE["failed"].append("grok_process")
    return _traced("grok_process", found)


NAME_MAX = 64   # longest agent or project name `--as` / POSTBOX_AS accepts, after cleaning


def explicit_identity(text, default_project=None):
    """The identity a person or agent states outright (`post --as grok@myproject ...`, or POSTBOX_AS), as (agent, project, None).

    It goes through the same cleaning as every address: lower case letters, digits and hyphens only, so a hostile value cannot put a
    newline, a quote or a shell character into a header or a reply command. 'grok' alone means the working folder's project.
    Three parts (agent@project@machine) are refused: which Mac a message comes from is always THIS Mac's own name, added when it sends.
    Raises ValueError, in words, for anything that is not an address (it never falls back to guessing)."""
    raw = str(text or "").strip()
    cleaned = clean_address(raw) if len(raw) <= 4 * NAME_MAX else "unknown"
    parts = cleaned.split("@")
    if cleaned == "unknown" or len(parts) > 2 or "unknown" in parts or any(len(p) > NAME_MAX for p in parts):
        if len(raw.split("@")) == 3 and cleaned != "unknown" and "unknown" not in parts:
            raise ValueError(f"{raw[:40]!r} names a Mac: who you are is only agent@project, this Mac adds its own name when it sends to another Mac")
        raise ValueError(f"{raw[:40]!r} is not who-you-are: use agent or agent@project, each up to {NAME_MAX} letters, digits or hyphens, for example grok@myproject")
    project = parts[1] if len(parts) == 2 else norm(default_project) if default_project else project_of(parts[0], None, os.getcwd())
    if project == "unknown" or len(project) > NAME_MAX:
        raise ValueError(f"cannot tell the project of {parts[0]!r} from the working folder: say it outright, for example {parts[0]}@myproject")
    return parts[0], project, None


def resolve_sender(flag=None):
    """((agent, project, pid_or_None), how) for the sender of this command, or (None, "") when it cannot be told.

    Order: the `--as` option (a deliberate choice for this one command) beats the session `post` finds itself running in, which
    beats POSTBOX_AS. POSTBOX_AS is only the fallback for when nothing is detected: a stray POSTBOX_AS in a shell profile can never
    make a Claude or Codex session send under another name. A stated identity that is not a valid address is an error, not a fall back."""
    if flag is not None:
        IDENTITY_TRACE.update(method="flag_as", failed=[])
        IDENTITY_PROJECT["info"] = None
        return explicit_identity(flag), "the --as option"
    me = my_identity()
    if me:
        return me, "found from the session post is running in"
    env = os.environ.get("POSTBOX_AS", "")
    if env.strip():
        IDENTITY_TRACE["method"] = "postbox_as"
        return explicit_identity(env), "POSTBOX_AS"
    return None, ""


# ---------------------------------------------------------------- Grok
# Grok is recognised by its own list of open sessions (GROK_HOME/active_sessions.json: session id, process number, folder) joined to the
# process tree: a command is a Grok session's when a `grok` process that list names is one of its ancestors. The project is the folder
# Grok recorded for that session, never the shell's folder. This is sender discovery, not authentication (the same local-trust rule as
# every other door). Verified from Grok's own files and binary; whether a real Grok shell command is a descendant of that process is
# checked by `post whoami` run from inside Grok.
GROK_COMM_RE = re.compile(r"^grok($|-)")      # `grok`, or a versioned file name such as grok-1.0.44-macos-aarch64


def grok_home() -> Path:
    """Grok's own folder: GROK_HOME (Grok's documented override) else ~/.grok."""
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")


def grok_rows():
    """The usable rows of Grok's list of open sessions, newest first: {session_id, pid, cwd, opened_at}. Anything malformed is skipped."""
    try:
        data = json.loads((grok_home() / "active_sessions.json").read_text())
    except (OSError, ValueError):
        return []
    rows = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        pid, cwd, sid = item.get("pid"), item.get("cwd"), item.get("session_id")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            continue
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or not cwd.strip("/") or not isinstance(sid, str) or not sid:
            continue
        rows.append({"session_id": sid, "pid": pid, "cwd": cwd, "opened_at": str(item.get("opened_at") or "")})
    return sorted(rows, key=lambda r: r["opened_at"], reverse=True)


def _grok_process(pid) -> bool:
    """True when this process number is a running `grok` (a stale row whose number was reused by another program is not)."""
    return bool(GROK_COMM_RE.match(os.path.basename(_run(["ps", "-p", str(pid), "-o", "comm="]).strip())))


def grok_sessions():
    """Live Grok sessions on this Mac, newest first: the rows whose process is still a running grok."""
    return [_session("grok", r["pid"], r["cwd"], r["session_id"])
            for r in grok_rows() if pid_alive(r["pid"]) and _grok_process(r["pid"])]


def grok_identity():
    """(agent, project, pid) when this process runs under a Grok session Grok itself lists, else None. Bounded walk up the process tree."""
    rows = {r["pid"]: r for r in grok_rows()}
    if not rows:
        return None   # no Grok open: nothing to look for, and no process listing is spent on it
    seen, pid = set(), os.getpid()
    for _ in range(20):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        fields = _run(["ps", "-p", str(pid), "-o", "ppid=,comm="]).strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            return None
        parent, executable = fields
        if pid in rows and GROK_COMM_RE.match(os.path.basename(executable)):
            cwd = rows[pid]["cwd"]
            return "grok", _identity_project("grok", pid, cwd), pid
        pid = int(parent)
    return None


def all_sessions():
    """Every live agent session this Mac can deliver to."""
    return (claude_sessions() + (codex_sessions() if doors.enabled("codex") else []) + (grok_sessions() if doors.enabled("grok") else []))


# ---------------------------------------------------------------- Inbox (for an agent that cannot be woken from outside: Grok)
# A folder per agent@project of small files, one per unread message. The relay writes, `post inbox` reads and deletes.
# Capped in count and size, and a message expires after MAX_AGE_S exactly like queued mail. Private to this user.
INBOX_MAX_MESSAGES = int(os.environ.get("POSTBOX_INBOX_MAX", "50"))
INBOX_MAX_BYTES = MAX_TEXT_BYTES * 2          # one stored message: the text plus the reading instructions that wrap it
INBOX_NAME_RE = re.compile(r"^(\d{13})-([0-9a-f]{12})\.msg$")


class InboxFull(Exception):
    """The inbox already holds INBOX_MAX_MESSAGES unread messages."""


def inbox_root() -> Path:
    return Path(os.environ.get("POSTBOX_INBOX_DIR") or support_dir() / "inbox")


def inbox_dir(agent, project) -> Path:
    """One inbox per agent@project. Both names are cleaned, so no value can point outside the inbox root."""
    return inbox_root() / f"{norm(agent)}.{canon_project(project)}"


def _unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


def inbox_list(agent, project, now=None):
    """The unread, unexpired message files of one inbox, oldest first. Expired and stray files are removed on the way."""
    now = time.time() if now is None else now
    folder = inbox_dir(agent, project)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    live = []
    for name in names:
        path = folder / name
        m = INBOX_NAME_RE.match(name)
        if not m:
            if name.startswith(".tmp-"):   # a write that never finished
                try:
                    if now - path.stat().st_mtime > MAX_AGE_S:
                        _unlink(path)
                except OSError:
                    pass
            continue
        if now - int(m.group(1)) / 1000 > MAX_AGE_S:
            _unlink(path)
            telemetry.emit("post_expire", "inbox", status="warning", kind="inbox", count=1, agent=norm(agent), project=norm(project),
                           age_s=int(now - int(m.group(1)) / 1000))
        elif path.is_file() and not path.is_symlink():
            live.append(path)
    return live


def inbox_put(agent, project, text, now=None):
    """Store one message. Raises InboxFull at the cap (the sender's message then stays queued and is retried), ValueError if too big."""
    now = time.time() if now is None else now
    data = str(text).encode("utf-8", "replace")
    if len(data) > INBOX_MAX_BYTES:
        raise ValueError(f"message too big for an inbox ({len(data)} bytes, the limit is {INBOX_MAX_BYTES})")
    folder = inbox_dir(agent, project)
    inbox_root().mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=True)
    waiting = len(inbox_list(agent, project, now))
    if waiting >= INBOX_MAX_MESSAGES:
        telemetry.emit("post_inbox", "inbox", status="warning", op="full", agent=norm(agent), project=norm(project), depth=waiting,
                       limit=INBOX_MAX_MESSAGES)
        raise InboxFull(f"inbox full ({INBOX_MAX_MESSAGES} unread messages); it is retried once the session reads some")
    tmp = folder / f".tmp-{uuid.uuid4().hex[:12]}"
    final = folder / f"{int(now * 1000):013d}-{uuid.uuid4().hex[:12]}.msg"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, final)   # the reader never sees half a message
    telemetry.emit("post_inbox", "inbox", op="put", agent=norm(agent), project=norm(project), depth=waiting + 1, bytes=len(data))
    return final


def inbox_depths():
    """{"agent.project": unread count} for every inbox, without removing anything (telemetry reads this; inbox_list would delete expired files)."""
    depths = {}
    try:
        for entry in sorted(inbox_root().iterdir()):
            if entry.is_dir():
                depths[entry.name] = sum(1 for name in os.listdir(entry) if INBOX_NAME_RE.match(name))
    except OSError:
        pass
    return depths


def inbox_take(agent, project, emit, now=None):
    """Hand every unread message to emit(number, text), oldest first, deleting each ONLY after emit returned: a failed print loses nothing.
    Returns how many were handed over. A file that is too big or already taken by another reader is skipped."""
    count = 0
    for path in inbox_list(agent, project, now):
        try:
            if path.stat().st_size > INBOX_MAX_BYTES:
                _unlink(path)
                continue
            text = path.read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        count += 1
        emit(count, text)
        _archive_read(path, now)
    return count


READ_DIR = "read"   # drained mail moves here (inside its inbox folder) and stays readable for MAX_AGE_S: `post inbox --history`
READ_NAME_RE = re.compile(r"^(\d{13})-(\d{13}-[0-9a-f]{12}\.msg)$")   # <read time ms>-<arrival name>


def _archive_read(path, now=None):
    """Move a message that has just been read into the folder's read/ archive (named with the time it was read). If that fails, delete it as before:
    reading must never leave the same message to be shown again."""
    now = time.time() if now is None else now
    try:
        folder = path.parent / READ_DIR
        folder.mkdir(mode=0o700, exist_ok=True)
        os.replace(path, folder / f"{int(now * 1000):013d}-{path.name}")
    except OSError:
        _unlink(path)


def inbox_history(agent, project, now=None):
    """[(read_time, arrival_time, path)] for mail already read in the last MAX_AGE_S seconds, oldest read first. Older archived files are removed on the way."""
    now = time.time() if now is None else now
    folder = inbox_dir(agent, project) / READ_DIR
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        path = folder / name
        m = READ_NAME_RE.match(name)
        if not m:
            continue
        read_at = int(m.group(1)) / 1000
        if now - read_at > MAX_AGE_S:
            _unlink(path)
        elif path.is_file() and not path.is_symlink():
            out.append((read_at, int(m.group(2)[:13]) / 1000, path))
    return out


def inbox_sweep(now=None):
    """Remove expired messages from every inbox (the relay runs this on its heartbeat). Returns how many files were removed."""
    root, removed = inbox_root(), 0
    try:
        folders = [p.name for p in root.iterdir() if p.is_dir()]
    except OSError:
        return 0
    for name in folders:
        agent, _, project = name.partition(".")
        before = len(os.listdir(root / name)) if (root / name).is_dir() else 0
        inbox_list(agent, project, now)
        inbox_history(agent, project, now)   # also expires the read/ archive
        removed += max(0, before - len(os.listdir(root / name)))
    return removed


# ---------------------------------------------------------------- Codex
CODEX_SUBCOMMANDS = {
    "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server", "remote-control", "app",
    "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "queue", "archive", "delete",
    "migrate-rollouts", "unarchive", "cloud", "exec-server", "features", "help", "agents",
}
CODEX_VALUE_FLAGS = {
    "-C", "--cd", "-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval", "-c", "--config", "-p", "--profile",
    "-i", "--image", "--enable", "--disable", "--remote", "--remote-auth-token-env", "--add-dir", "--local-provider",
}
CODEX_THREAD_SLACK_S = float(os.environ.get("POSTBOX_CODEX_THREAD_SLACK_S", "5"))   # clock slack between a screen's start and its thread row
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def codex_screen_args(argv):
    """For the arguments after `codex`: (is_interactive_screen, resumed_thread_id_or_None)."""
    i, words = 0, []
    while i < len(argv):
        a = argv[i]
        if a.startswith("-"):
            i += 2 if (a in CODEX_VALUE_FLAGS and "=" not in a) else 1
            continue
        words.append(a)
        i += 1
    if words and words[0] in CODEX_SUBCOMMANDS:
        return False, None
    thread = None
    if words and words[0] in ("resume", "fork") and len(words) > 1 and UUID_RE.match(words[1]):
        thread = words[1]
    return True, thread


def codex_state_db():
    found = sorted(glob.glob(os.path.expanduser("~/.codex/state_*.sqlite")))
    return found[-1] if found else None


def codex_thread_info(thread_id=None, cwd=None, active_only=False):
    """(thread_id, cwd) from Codex's own thread table, read-only. By id, else newest live thread in cwd."""
    import sqlite3
    db = codex_state_db()
    if not db:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        if thread_id:
            query = "select id, cwd from threads where id=?"
            if active_only:
                query += " and archived=0"
            row = con.execute(query, (thread_id,)).fetchone()
        else:
            own = " and (thread_source is null or thread_source != 'subagent')" if "thread_source" in _codex_columns(con) else ""
            row = con.execute(f"select id, cwd from threads where cwd=? and archived=0{own} order by updated_at desc limit 1", (cwd,)).fetchone()
        con.close()
        return row
    except Exception:
        return None


def _codex_columns(con):
    return {r[1] for r in con.execute("pragma table_info(threads)")}


def codex_user_threads(cwd, since):
    """[(id, created_at)]: the person's own conversations (thread_source 'user', never a subagent's) in this folder created at or after
    `since`, newest first. [] when Codex's table cannot tell a subagent's thread from the person's (an older Codex) or cannot be read."""
    import sqlite3
    db = codex_state_db()
    if not db or not cwd:
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        if not {"thread_source", "created_at"} <= _codex_columns(con):
            con.close()
            return []
        rows = con.execute("select id, created_at from threads where cwd=? and archived=0 and thread_source='user' and created_at>=?"
                           " order by created_at desc", (cwd, int(since))).fetchall()
        con.close()
        return rows
    except Exception:
        return []


def codex_user_thread_snapshot():
    """The eligible user threads in one read, or None when this Codex table cannot distinguish subagents."""
    import sqlite3
    db = codex_state_db()
    if not db:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        if not {"thread_source", "created_at"} <= _codex_columns(con):
            con.close()
            return None
        rows = con.execute("select id, cwd, updated_at, created_at from threads "
                           "where archived=0 and thread_source='user' order by created_at desc").fetchall()
        con.close()
        return rows
    except Exception:
        return None


def codex_thread_created(thread_id):
    import sqlite3
    db = codex_state_db()
    if not db:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute("select created_at from threads where id=?", (thread_id,)).fetchone() if "created_at" in _codex_columns(con) else None
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _codex_unique_matching(candidates):
    """The sole full screen-to-thread matching, else None.  A bounded search fails closed on a large ambiguous component."""
    solutions, visits = [], 0

    def search(pending, chosen, used):
        nonlocal visits
        if len(solutions) > 1 or visits >= 10000:
            return
        if not pending:
            solutions.append(chosen)
            return
        options = [(len([tid for tid in own if tid not in used]), pid, own) for pid, own in pending.items()]
        _count, pid, own = min(options)
        for tid in sorted(tid for tid in own if tid not in used):
            visits += 1
            search({other: choices for other, choices in pending.items() if other != pid},
                   {**chosen, pid: tid}, used | {tid})
            if len(solutions) > 1 or visits >= 10000:
                return

    search(candidates, {}, set())
    return solutions[0] if len(solutions) == 1 and visits < 10000 else None


def codex_thread_updated(thread_id):
    """Seconds-since-epoch the thread was last updated, from Codex's thread table (read-only), else None."""
    import sqlite3
    db = codex_state_db()
    if not db or not thread_id:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute("select updated_at from threads where id=?", (thread_id,)).fetchone()
        con.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def process_age_s(pid):
    """Seconds since the process started (ps etime: [[dd-]hh:]mm:ss, no locale in it), else None."""
    text = _run(["ps", "-p", str(pid), "-o", "etime="]).strip()
    m = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", text)
    if not m:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


# A Codex screen reads its inbox only between tasks, so a message sent while it is mid-task waits.
# Codex writes "task_started" / "task_complete" (and "turn_aborted" when a task is interrupted) into the
# thread's own log (`rollout_path` in the thread table). The newest marker says whether it is busy.
CODEX_MARK_RE = re.compile(rb'"type":"event_msg","payload":\{"type":"(task_started|task_complete|turn_aborted)"')
CODEX_TAIL_CHUNK = 1 << 20     # read the log backwards, 1 MB at a time
CODEX_TAIL_MAX = 64 << 20      # give up (say nothing) if no marker turns up in the last 64 MB


def codex_thread_rollout(thread_id):
    """Path of a Codex thread's own log file, from its thread table (read-only), else None."""
    import sqlite3
    db = codex_state_db()
    if not db or not thread_id:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        row = con.execute("select rollout_path from threads where id=?", (thread_id,)).fetchone()
        con.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _codex_marker_time(line):
    """Epoch seconds a marker line says its task began, else None."""
    from datetime import datetime, timezone
    try:
        event = json.loads(line)
        started = (event.get("payload") or {}).get("started_at")
        if isinstance(started, (int, float)) and started > 1e9:
            return float(started)
        return datetime.strptime(event["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def codex_busy_seconds(thread_id, now=None):
    """Seconds the Codex thread has been in its CURRENT task, or None when it is idle or cannot be told.

    The log can be hundreds of MB, so it is read backwards in chunks and only until the newest marker
    turns up. Unknown (no log, no marker, unreadable) is None: the sender is never told what we could not see.
    """
    path = codex_thread_rollout(thread_id)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            pos, scanned, carry = fh.tell(), 0, b""
            while pos > 0 and scanned < CODEX_TAIL_MAX:
                step = min(CODEX_TAIL_CHUNK, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step) + carry
                scanned += step
                lines = chunk.split(b"\n")
                carry = b""
                if pos > 0:              # the first line may be cut off by the chunk edge: finish it next round
                    carry, lines = lines[0], lines[1:]
                for line in reversed(lines):
                    m = CODEX_MARK_RE.search(line) if b'"payload":{"type":"' in line else None
                    if not m:
                        continue
                    if m.group(1) != b"task_started":
                        return None      # newest marker says the task ended (or was interrupted): idle
                    began = _codex_marker_time(line)
                    if began is None:
                        return None
                    return max(0, int((time.time() if now is None else now) - began))
    except OSError:
        return None
    return None


def ago(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    days, hours = seconds // 86400, seconds % 86400 // 3600
    return f"{days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")


def native_sender_identity(thread_id):
    """Bind runtime-provided identity to this process's native ancestor and exact active row."""
    if not UUID_RE.fullmatch(thread_id):
        return None
    seen, pid = set(), os.getpid()
    for _ in range(20):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        fields = _run(["ps", "-p", str(pid), "-o", "ppid=,comm="]).strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            return None
        parent, executable = fields
        if os.path.isabs(executable) and os.path.basename(executable) == "codex":
            info = codex_thread_info(thread_id=thread_id, active_only=True)
            if not info or info[0] != thread_id:
                return None
            cwd = info[1]
            if not isinstance(cwd, str) or not os.path.isabs(cwd) or not cwd.strip("/"):
                return None
            return "codex", _identity_project("codex", pid, cwd), pid
        pid = int(parent)
    return None


def process_table():
    """{pid: (ppid, argv)} for every process, from ps."""
    import shlex
    table = {}
    for line in _run(["ps", "-axo", "pid=,ppid=,command="], timeout=8).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        try:
            argv = shlex.split(parts[2])
        except ValueError:
            continue
        table[int(parts[0])] = (int(parts[1]), argv)
    return table


def _codex_wrapper_args(argv):
    """argv of the `node .../codex` wrapper (the launcher a person types) -> args after it, else None."""
    if len(argv) > 1 and argv[0].rsplit("/", 1)[-1] == "node" and argv[1].endswith("/codex"):
        return argv[2:]
    return None


def codex_sessions(table=None):
    """Live interactive Codex screens on this Mac, found from the process list plus Codex's thread table.

    A screen started with plain `codex` has no thread id in its arguments, so its thread is the newest one in its folder. Until the screen
    has made its own thread row, that newest one is an OLDER conversation: handing mail to it loses the mail. Such a screen is listed
    with ready=False (not a delivery target) when the newest thread was last updated before the screen started."""
    table = process_table() if table is None else table
    screens = []
    for pid, (_ppid, argv) in sorted(table.items()):
        args = _codex_wrapper_args(argv)
        if args is None:
            continue
        interactive, thread = codex_screen_args(args)
        if interactive:
            screens.append((pid, thread, cwd_of(pid)))
    claimed = {t for _p, t, _c in screens if t}
    threads = codex_user_thread_snapshot()
    now = time.time()
    starts = {pid: (now - age - CODEX_THREAD_SLACK_S) if (age := process_age_s(pid)) is not None else None
              for pid, _thread, _cwd in screens}
    by_id = {tid: (cwd, updated, created) for tid, cwd, updated, created in threads} if threads is not None else {}

    # Reserve arguments before assigning a conversation to any idless screen.  A process-list order must never let a plain
    # `codex` screen steal a conversation another screen explicitly resumed.
    explicit = {}
    for pid, thread, proc_cwd in screens:
        if not thread:
            continue
        moved = None
        if threads is not None and starts[pid] is not None and proc_cwd:
            launched = by_id.get(thread, ("", 0, 0))[2] or 0
            newer = [tid for tid, cwd, _updated, created in threads
                     if cwd == proc_cwd and tid != thread and tid not in claimed and created >= starts[pid] and created > launched]
            if newer:
                rivals = [p for p, _t, cwd in screens if p != pid and cwd == proc_cwd]
                moved = "" if rivals else newer[0]
        elif threads is None:
            moved = _codex_moved_to(pid, thread, proc_cwd, claimed, screens)
        explicit[pid] = moved if moved else thread
        if moved == "":
            explicit[pid] = ""
    reserved = claimed | {thread for thread in explicit.values() if thread}

    # An idless screen owns only a candidate that belongs to exactly one idless screen.  Siblings that could own the same
    # conversation remain visible but unready, so queued mail cannot land in the wrong window.
    idless = [(pid, proc_cwd) for pid, thread, proc_cwd in screens if not thread]
    candidates = {}
    if threads is not None:
        for pid, proc_cwd in idless:
            started = starts[pid]
            if started is not None and proc_cwd:
                own = [tid for tid, cwd, _updated, created in threads if cwd == proc_cwd and tid not in reserved and created >= started]
                if own:
                    candidates[pid] = own

    # Only resolve an entire connected component when its full bipartite matching is unique.  A locally exclusive-looking edge
    # can still be optional when its screen has alternatives, so component-level proof is required before delivering mail.
    assignments = {}
    owners = {}
    for pid, own in candidates.items():
        for tid in own:
            owners.setdefault(tid, set()).add(pid)
    unseen = set(candidates)
    while unseen:
        component, threads, stack = set(), set(), [unseen.pop()]
        while stack:
            pid = stack.pop()
            if pid in component:
                continue
            component.add(pid)
            for tid in candidates[pid]:
                threads.add(tid)
                for rival in owners[tid]:
                    if rival not in component:
                        stack.append(rival)
        unseen -= component
        matched = _codex_unique_matching({pid: candidates[pid] for pid in component})
        if matched:
            assignments.update(matched)

    found = []
    for pid, thread, proc_cwd in screens:
        if thread:
            thread = explicit[pid]
            if not thread:
                found.append(_session("codex", pid, proc_cwd or "", "", ready=False))
                continue
        elif pid in assignments:
            thread = assignments[pid]
        elif threads is not None:
            found.append(_session("codex", pid, proc_cwd or "", "", ready=False))
            continue
        if threads is not None:
            # The snapshot deliberately contains only active user threads for automatic matching.  An explicit `resume <id>`
            # is different: it has always been allowed to address its exact thread, including an archived conversation.
            info = (thread, *by_id[thread][:1]) if thread in by_id else codex_thread_info(thread_id=thread)
        else:
            info = codex_thread_info(thread_id=thread) if thread else codex_thread_info(cwd=proc_cwd)
        if not info:
            if not thread and proc_cwd:   # a screen with no conversation at all in its folder yet
                found.append(_session("codex", pid, proc_cwd, "", ready=False))
            continue
        tid, tcwd = info
        cwd = tcwd or proc_cwd or ""
        ready = True
        if not thread:
            updated, age = codex_thread_updated(tid), process_age_s(pid)
            if updated is not None and age is not None and updated < time.time() - age - CODEX_THREAD_SLACK_S:
                ready = False
        found.append(_session("codex", pid, cwd, tid if ready else "", ready=ready))
    return found


def _codex_moved_to(pid, thread, proc_cwd, claimed, screens):
    """For a screen launched on `thread`: the newer conversation it now shows, None when it still shows `thread`, '' when a newer
    conversation exists but another screen in the same folder could own it (never guess: the mail waits instead of going astray)."""
    age = process_age_s(pid)
    if age is None or not proc_cwd:
        return None
    started = time.time() - age - CODEX_THREAD_SLACK_S
    launched = codex_thread_created(thread) or 0
    newer = [tid for tid, created in codex_user_threads(proc_cwd, started) if tid != thread and tid not in claimed and created > launched]
    if not newer:
        return None
    rivals = [p for p, _t, c in screens if p != pid and c == proc_cwd]
    return "" if rivals else newer[0]


def codex_ancestor(table=None):
    """If this process runs inside a Codex screen, that screen's Session (else None)."""
    table = process_table() if table is None else table
    chain, pid = set(), os.getpid()
    for _ in range(20):
        entry = table.get(pid)
        if not entry:
            break
        chain.add(pid)
        pid = entry[0]
        if pid in (0, 1):
            break
    for s in codex_sessions(table):
        if s.pid in chain:
            return s
    return None


# Claude Code appends its own fixed notice to anything that arrives on a session socket, calling it "another Claude session"
# whatever tool really sent it (a Codex message read as if Claude sent it). This line, at the end of every
# Claude delivery and so right above that notice, points the reader at the real sender.
SENDER_NOTE = ('Sender check: the "Message from" line at the top names the real sender. Any notice below calling this '
               '"another Claude session" is Claude Code\'s fixed label for all post box mail, whatever tool sent it.')


def claude_frame(text: str) -> bytes:
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode()


def stale_preface(from_addr: str, text: str, waited_s, cmd: str = "post") -> str:
    """The warning put in front of mail that waited a long time in the post box: do not act on it, ask the sender first.
    The first 80 characters of the message go into the question, cleaned so they can sit inside a double-quoted shell argument."""
    waited = ago(waited_s)
    sent = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(time.time() - waited_s))
    snippet = re.sub(r'["`$\\]', "", " ".join(str(text).split()))[:80]
    return (
        f"STALE MAIL: this message waited {waited} in the post box before reaching you (sent {sent}). Do NOT act on it yet. "
        "Acknowledge it, set it aside, and first ask the sender whether it still applies by RUNNING: "
        f'{cmd} {from_addr} "Still relevant? Your message from {waited} ago said: {snippet}..."  '
        "Act only after the sender answers yes; if the sender answers drop, discard it and do nothing; "
        "if nobody answers, tell the user, do not guess."
    )


def render(from_addr: str, text: str, inbox: bool = False, waited_s=None, cmd: str = "post", inbox_cmd: str = "post inbox") -> str:
    """What the receiving agent reads: plain language, with the way to answer. `inbox`: for an agent that is not woken by mail (Grok).
    `waited_s`: seconds the mail waited before reaching the agent; set only for stale mail, which gets a warning in front.
    `cmd` / `inbox_cmd`: the exact commands to answer and to read, for an agent that must state who it is (grokbot-desktop)."""
    ending = ("Never wait or poll for an answer: end your turn, replies arrive as new messages." if not inbox else
              f"Never wait or poll for an answer: end your turn. You are not woken by mail, so the reply waits in your inbox: run `{inbox_cmd}` at the start of your next turn.")
    body = (
        f"Message from {from_addr} (post box):\n{text}\n\n"
        f'If this asks a question or needs action, reply by RUNNING this shell command (typing it in chat sends nothing): {cmd} {from_addr} "your reply". '
        "If it only informs you, do not reply: the sender was already told it was delivered. " + ending
    )
    return body if waited_s is None else stale_preface(from_addr, text, waited_s, cmd) + "\n\n" + body


class ClaudeDoor:
    agent = "claude"
    per_session = True   # several sessions can be open in one project: a message goes to ONE of them (named, or the only one), never to all

    def resolve(self, project, exclude_pid=None, sessions=None):
        sessions = claude_sessions() if sessions is None else sessions
        return [s for s in sessions if same_project(s.project, project) and s.pid != exclude_pid]

    def deliver(self, session, text, timeout=3.0):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(session.target)
            s.sendall(claude_frame(f"{text}\n\n{SENDER_NOTE}"))
        finally:
            s.close()


class CodexDoor:
    agent = "codex"
    per_session = True

    def resolve(self, project, exclude_pid=None, sessions=None):
        sessions = codex_sessions() if sessions is None else sessions
        return [s for s in sessions if same_project(s.project, project) and s.pid != exclude_pid and s.ready]

    def waiting(self, project, exclude_pid=None, sessions=None):
        """Screens in this project that are open but have no conversation of their own yet (mail for them is kept, not delivered)."""
        sessions = codex_sessions() if sessions is None else sessions
        return [s for s in sessions if same_project(s.project, project) and s.pid != exclude_pid and not s.ready]

    def busy_note(self, session, now=None):
        """Sentence for the sender when this Codex screen is mid-task (it reads the message only afterwards), else ''.

        Ask BEFORE delivering: an idle Codex starts a task the moment the message lands, which would look busy.
        """
        seconds = codex_busy_seconds(session.target, now)
        if seconds is None:
            return ""
        return f"{session.label} is mid-task (started {ago(seconds)} ago) and reads this only when its task ends"

    def watch(self, session, text, from_addr, from_session=None):
        codex_watch_add(session, text, from_addr, from_session)

    def deliver(self, session, text, timeout=25.0):
        r = subprocess.run(["codex", "queue", "--thread", session.target, "--message", text],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "codex queue failed").strip()[:200])


# Proof of delivery for Codex. `codex queue` answers yes even when the conversation it queued into is one nobody has open
# (mail to a Codex screen can sit unread that way), so after every Codex delivery the relay watches that conversation's own log
# and calls the message `seen` once its text appears there, or `unseen` (an alarm) when it does not: after CODEX_SEEN_AFTER_S while Codex
# is idle, or after CODEX_SEEN_MAX_S however busy it is. The watch list is a small file so a relay restart does not forget it.
CODEX_SEEN_AFTER_S = float(os.environ.get("POSTBOX_CODEX_SEEN_S", "600"))
CODEX_SEEN_MAX_S = float(os.environ.get("POSTBOX_CODEX_SEEN_MAX_S", "7200"))
CODEX_SNIPPET = 100


def _codex_watch_file() -> Path:
    return Path(os.environ.get("POSTBOX_CODEX_WATCH") or support_dir() / "codex-watch.json")


def _flat(text) -> str:
    return " ".join(str(text).split())


def codex_watch_load() -> list:
    try:
        rows = json.loads(_codex_watch_file().read_text())
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def _codex_watch_save(rows) -> None:
    path = _codex_watch_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def codex_watch_add(session, text, from_addr, from_session=None, now=None) -> None:
    """Start watching for `text` in the conversation `session` points at, from the log's current end (older lines never count)."""
    rollout = codex_thread_rollout(session.target)
    try:
        offset = os.path.getsize(rollout) if rollout else None
    except OSError:
        offset = None
    rows = codex_watch_load()
    rows.append({"thread": session.target, "rollout": str(rollout) if rollout else "", "offset": offset,
                 "snippet": _flat(text)[:CODEX_SNIPPET], "to": session.label, "frm": from_addr, "from_session": from_session or "",
                 "sent": time.time() if now is None else now})
    _codex_watch_save(rows)


def _codex_log_has(rollout, offset, snippet) -> bool:
    with open(rollout, "rb") as f:
        f.seek(offset or 0)
        for raw in f:
            if not raw.strip():
                continue
            try:
                line = json.dumps(json.loads(raw), ensure_ascii=False)
                strings = json.loads(raw)
            except ValueError:
                continue
            probe = re.search(r"[A-Za-z0-9]{4,}", snippet)
            if probe and probe.group(0) not in line:   # cheap filter (a word with no characters JSON escapes) before the full search
                continue
            stack, parts = [strings], []
            while stack:
                v = stack.pop()
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, dict):
                    stack.extend(v.values())
                elif isinstance(v, list):
                    stack.extend(v)
            if any(snippet in _flat(x) for x in parts):
                return True
    return False


def codex_watch_check(now=None) -> list:
    """[(entry, 'seen'|'unseen')] for every watched message that is settled now; the settled ones leave the watch list. Never raises."""
    now = time.time() if now is None else now
    rows, keep, settled = codex_watch_load(), [], []
    for e in rows:
        try:
            if not e.get("rollout") or e.get("offset") is None or not os.path.exists(e["rollout"]):
                settled.append((e, "unseen"))
                continue
            if _codex_log_has(e["rollout"], e["offset"], e.get("snippet", "")):
                settled.append((e, "seen"))
                continue
            age = now - float(e.get("sent", now))
            busy = codex_busy_seconds(e.get("thread"), now) is not None
            if age >= CODEX_SEEN_MAX_S or (age >= CODEX_SEEN_AFTER_S and not busy):
                settled.append((e, "unseen"))
            else:
                keep.append(e)
        except Exception:  # noqa: BLE001 - a check that cannot read the log must still raise the alarm, not keep quiet
            settled.append((e, "unseen"))
    if settled:
        try:
            _codex_watch_save(keep)
        except OSError:
            pass
    return settled


class GrokDoor:
    """Nothing outside a Grok screen can wake it, so its mail goes into its inbox and the session reads it with `post inbox`
    (at the start of its turns, or when its `post inbox --watch` monitor rings). `delivered` therefore means: a live Grok session
    in that project exists and the message is waiting in its inbox, and the sender is told plainly that it is read later, not now."""
    agent = "grok"

    def resolve(self, project, exclude_pid=None, sessions=None):
        sessions = grok_sessions() if sessions is None else sessions
        match = [s for s in sessions if same_project(s.project, project) and s.pid != exclude_pid]
        return match[:1]   # newest first, and one inbox per project: two targets would store the same message twice

    def busy_note(self, session, now=None):
        """The sentence the relay appends to `delivered` (asked before delivering, like the Codex mid-task note)."""
        return f"{session.label} cannot be woken: it reads this from its inbox (post inbox) when its next turn starts, so an answer can take a while"

    def render(self, from_addr, text, waited_s=None):
        return render(from_addr, text, inbox=True, waited_s=waited_s)

    def deliver(self, session, text, timeout=None):
        inbox_put(self.agent, session.project, text)


class InboxSession(Session):
    """The one synthetic target of an agent the box cannot discover: it names an inbox, not a process."""

    @property
    def label(self) -> str:
        return f"{self.agent}@{self.project} (inbox)"


class GrokbotDesktopDoor:
    """The agents inside Grok Bot desktop (a different app from the `grok` command line tool). Grok Bot desktop keeps no
    session list the box can read, so there is no discovery: it is known only by stating who it is (`post --as grokbot-desktop@<project>`
    or POSTBOX_AS), it cannot be woken, and it reads its mail with `POSTBOX_AS=grokbot-desktop@<project> post inbox`. A message for
    grokbot-desktop@<project> is therefore always `delivered` into that inbox (never "nobody reachable"), with the note that it is read later."""
    agent = "grokbot-desktop"
    inbox_only = True           # post who lists it from its inbox, not from a session list
    render_with_project = True  # the reading and answering commands name the project, because the agent has to state it

    def resolve(self, project, exclude_pid=None, sessions=None):
        return [InboxSession(self.agent, 0, "", canon_project(project), "inbox")]

    def busy_note(self, session, now=None):
        return f"{self.agent} cannot be woken: it reads this with `POSTBOX_AS={self.agent}@{session.project} post inbox` when its next turn starts"

    def render(self, from_addr, text, waited_s=None, project=None):
        me = f"{self.agent}@{project}" if project else f"{self.agent}@<project>"
        return render(from_addr, text, inbox=True, waited_s=waited_s, cmd=f"post --as {me}", inbox_cmd=f"POSTBOX_AS={me} post inbox")

    def deliver(self, session, text, timeout=None):
        inbox_put(self.agent, session.project, text)


_ALL_DOORS = {"claude": ClaudeDoor, "codex": CodexDoor, "grok": GrokDoor, "grokbot-desktop": GrokbotDesktopDoor}
DOORS = {name: cls() for name, cls in _ALL_DOORS.items() if doors.enabled(name)}   # only the doors postbox/doors.py (or POSTBOX_DOORS) switches on


def inbox_only_agents():
    """[(agent, project, waiting)] for every agent whose door has no session list (grokbot-desktop) and whose inbox exists, sorted. post who shows these."""
    names = {a for a, d in DOORS.items() if getattr(d, "inbox_only", False)}
    found = []
    for name, waiting in sorted(inbox_depths().items()):
        agent, _, project = name.partition(".")
        if agent in names and project:
            found.append((agent, project, waiting))
    return found


# ---------------------------------------------------------------- choosing ONE session, holding a message that did not say which
def choose_sessions(targets, want):
    """Which of the project's sessions gets a message. Returns (outcome, chosen):
      one        no name given and exactly one session is open: it gets the message
      named      a name was given and exactly one session has it (name or short id): it gets the message
      several    no name given and 2 or more sessions are open: nobody gets it, the message is held (chosen = all of them)
      ambiguous  a name was given and 2 or more sessions have it: held, and the sender is told to use the short id (chosen = those)
      unknown    a name was given and no open session has it: nobody gets it (chosen = [])
      none       nobody is open (chosen = [])"""
    targets = list(targets)
    if want:
        matched = [t for t in targets if t.matches(want)]
        if len(matched) == 1:
            return "named", matched
        return ("ambiguous", matched) if matched else ("unknown", [])
    if not targets:
        return "none", []
    return ("one", targets) if len(targets) == 1 else ("several", targets)


def session_tokens(targets):
    """[(token, session)] where token is what to type after '#' to reach exactly that session: its name when no other open session has the
    same one, else its short id (an id is the same for a re-opened conversation but always differs between two open sessions)."""
    targets = list(targets)
    counts = defaultdict(int)
    for t in targets:
        counts[t.handle] += 1
    return [(t.handle if counts[t.handle] == 1 else (t.sid or t.handle), t) for t in targets]


def session_names(targets, limit=12) -> str:
    """'algorithm, project-a (id 3fa9c1)' for a sentence: every open session by the token that reaches it."""
    toks = session_tokens(targets)
    shown = [f"{tok}" + (f" (pid {t.pid})" if sum(1 for x, _ in toks if x == tok) > 1 else "") for tok, t in toks[:limit]]
    return ", ".join(shown) + (f" and {len(toks) - limit} more" if len(toks) > limit else "")


HOLD_MAX = int(os.environ.get("POSTBOX_HOLD_MAX", "200"))        # most held messages kept at once; more are refused (a flood cannot fill the disk)
HOLD_FILE_MAX = MAX_TEXT_BYTES * 4 + 4096                         # one held record: the text (as JSON escapes) plus the sender facts
HOLD_ID_RE = re.compile(r"^[0-9a-f]{6,16}$")


class HoldFull(Exception):
    """HOLD_MAX messages are already held."""


def held_dir() -> Path:
    """Where held messages wait (one small file each, private to this user): $POSTBOX_HELD_DIR, else <support folder>/held."""
    return Path(os.environ.get("POSTBOX_HELD_DIR") or support_dir() / "held")


def _hold_path(hold_id) -> Path:
    return held_dir() / f"{hold_id}.json"


def _hold_expiry(rec) -> float:
    """A held message lives MAX_AGE_S from the time it was ORIGINALLY sent (the same 7 days as queued mail), not from when it was held."""
    try:
        return float(rec.get("sent_at") or rec.get("held_at") or 0) + MAX_AGE_S
    except (TypeError, ValueError):
        return 0.0


def _hold_read(path: Path):
    try:
        if path.stat().st_size > HOLD_FILE_MAX:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("text"), str) else None


def hold_sweep(now=None) -> int:
    """Remove held messages older than MAX_AGE_S (and unreadable stray files). Returns how many were removed."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(held_dir())
    except OSError:
        return 0
    for name in names:
        path = held_dir() / name
        if name.startswith(".tmp-"):
            try:
                if now - path.stat().st_mtime > 3600:
                    _unlink(path)
            except OSError:
                pass
            continue
        if not name.endswith(".json") or not HOLD_ID_RE.match(name[:-5]):
            continue
        rec = _hold_read(path)
        if rec is None or now > _hold_expiry(rec):
            _unlink(path)
            removed += 1
    return removed


def hold_list(now=None):
    """Held messages, oldest first, each the stored record plus its `id`. Expired ones are removed on the way."""
    now = time.time() if now is None else now
    hold_sweep(now)
    out = []
    try:
        names = os.listdir(held_dir())
    except OSError:
        return []
    for name in names:
        if name.endswith(".json") and HOLD_ID_RE.match(name[:-5]):
            rec = _hold_read(held_dir() / name)
            if rec is not None:
                out.append(dict(rec, id=name[:-5]))
    return sorted(out, key=lambda r: (float(r.get("held_at") or 0), r["id"]))


def hold_get(hold_id, now=None):
    """The held message with this id (record plus `id`), or None when it does not exist, expired or the id is not an id."""
    now = time.time() if now is None else now
    hold_id = str(hold_id or "").strip().lower()
    if not HOLD_ID_RE.match(hold_id):
        return None
    rec = _hold_read(_hold_path(hold_id))
    if rec is None:
        return None
    if now > _hold_expiry(rec):
        _unlink(_hold_path(hold_id))
        return None
    return dict(rec, id=hold_id)


def hold_delete(hold_id) -> bool:
    hold_id = str(hold_id or "").strip().lower()
    if not HOLD_ID_RE.match(hold_id):
        return False
    try:
        _hold_path(hold_id).unlink()
    except OSError:
        return False
    return True


def hold_put(rec, now=None) -> str:
    """Keep a message that could not be delivered because it did not say which session. `rec` carries msg_id (the stream's own message id, so
    a redelivery after a crash finds the same hold), agent, project, frm (sender address as it arrived), session (sender's name or ''), pid,
    text and sent_at. Returns its short id (6 hex characters, longer only when that id is taken by another message). Raises HoldFull."""
    import hashlib
    now = time.time() if now is None else now
    hold_sweep(now)
    folder = held_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(str(rec.get("msg_id") or uuid.uuid4().hex).encode()).hexdigest()
    for size in (6, 8, 10, 12, 16):
        hold_id = digest[:size]
        existing = _hold_read(_hold_path(hold_id))
        if existing is None:
            break
        if existing.get("msg_id") == rec.get("msg_id"):
            return hold_id          # the same message again (a redelivery): the same hold
    if sum(1 for n in os.listdir(folder) if n.endswith(".json")) >= HOLD_MAX:
        raise HoldFull(f"{HOLD_MAX} messages are already held")
    body = {k: rec.get(k) for k in ("msg_id", "agent", "project", "frm", "session", "pid", "text", "sent_at")}
    body["held_at"] = now
    body["sent_at"] = body.get("sent_at") or now
    tmp = folder / f".tmp-{uuid.uuid4().hex[:12]}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    os.replace(tmp, _hold_path(hold_id))
    return hold_id


def _with_machine(agent, project, machine):
    return f"{agent}@{project}" + (f"@{machine}" if machine else "")


def pick_commands(hold_id, agent, project, targets, machine=None):
    """One ready-to-run command per open session: `post pick <id> agent@project[@machine]#token`."""
    return [f"post pick {hold_id} {_with_machine(agent, project, machine)}#{tok}" for tok, _ in session_tokens(targets)]


def bounce_text(hold_id, agent, project, targets, machine=None, ambiguous_name=None) -> str:
    """What the sender is told when its message could not be delivered because it did not say which session (or the name it gave fits two).
    Plain words plus one ready command per session: the sender never retypes the message."""
    where = _with_machine(agent, project, machine)
    if ambiguous_name:
        why = f"{len(targets)} open {agent} sessions in {project} are both called '{ambiguous_name}', so it cannot tell which you meant."
    else:
        why = f"{len(targets)} {agent} sessions are open in {project} and the message did not say which one."
    cmds = "\n".join("  " + c for c in pick_commands(hold_id, agent, project, targets, machine))
    return (f"{why} Nothing was delivered. Your message is kept (id {hold_id}, {ago(MAX_AGE_S)}). Send it to ONE of them, without retyping it, by running:\n{cmds}\n"
            f"Open sessions: {session_names(targets)}. Next time name one: post {where}#<name> \"...\"")


def unknown_session_text(agent, project, want, targets, machine=None) -> str:
    where = _with_machine(agent, project, machine)
    return (f"no {agent} session called '{want}' is open in {project}. Open now: {session_names(targets)}. Nothing was delivered. "
            f"Send it again to one of them: post {where}#<name> \"...\"")


class PairLimiter:
    """Stop rule: at most `limit` messages per sender-to-recipient pair per window."""

    def __init__(self, limit=PAIR_LIMIT, window=PAIR_WINDOW_S):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)

    def allow(self, key, now=None) -> bool:
        now = time.time() if now is None else now
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
