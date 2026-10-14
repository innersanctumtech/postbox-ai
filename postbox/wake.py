"""Wake webhook: the doorbell for an agent that cannot be woken by mail (grokbot-desktop). Only used when that door is on (postbox/doors.py).

Its mail goes into an inbox and is read at the start of its next turn. When wake.json (in the post box's support folder, next to link.json)
has an entry for the agent, the relay also POSTs to that agent's webhook right after the inbox write, so a turn starts and reads it.
The inbox stays the source of truth: a wake that fails changes nothing about the delivery, it only tells the sender.

wake.json holds 1Password REFERENCES, never the secrets:
  {"grokbot-desktop": {"url_ref": "op://<vault>/<item>/<url-field>",
                       "key_ref": "op://<vault>/<item>/<key-field>", "timeout_s": 5,
                       "desks": {"project-a": "Project A Desk"},       # optional: sender project -> the exact Desk name sent as "desk" (null when none matches)
                       "desk_sender_projects": []}}                    # optional: projects whose own mail is never redirected to their own Desk (default none)
The values are fetched at call time and kept in memory for this process's life. The command that resolves a reference is, in order:
$POSTBOX_SECRET_CMD (a command that takes the reference as its last argument and prints the value), else ~/.claude/bin/op-cached when that file
exists, else the 1Password command line tool: `op read <reference>`. Neither the URL nor the key is ever logged or put in an answer:
reasons are short classes (timeout, dns, tls, http_500 ...).
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import lib

DEFAULT_TIMEOUT_S = 5.0
MAX_TIMEOUT_S = 15.0
RESOLVE_TIMEOUT_S = 10      # how long one secret lookup may take
RESOLVE_RETRY_S = 60        # after a failed lookup the secret is not asked for again for this long (so every delivery does not wait on it)
KEY_HEADER = "X-Postbox-Key"


@dataclass(frozen=True)
class WakeResult:
    ok: bool
    reason: str = ""         # empty when ok; otherwise a short class, never containing the URL or the key
    status: Optional[int] = None


class SecretUnavailable(Exception):
    """The 1Password reference could not be turned into a value."""


def wake_file() -> Path:
    return lib.support_dir() / "wake.json"


def load_wake() -> dict:
    """wake.json as {agent: entry}. Missing or unreadable means no wake for anyone (an empty dict); an entry that is not usable is dropped."""
    try:
        data = json.loads(wake_file().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {wake_key(*str(key).partition("@")[::2]): entry for key, entry in data.items()
            if isinstance(entry, dict) and str(entry.get("url_ref", "")).startswith("op://")
            and (str(entry.get("key_ref", "")).startswith("op://") or str(entry.get("header_ref", "")).startswith("op://"))}


def wake_key(agent: str, project: Optional[str] = None) -> str:
    """The wake.json key for an agent ("grokbot-desktop") or for one project of it ("grokbot-desktop@project-a": that Desk's own webhook)."""
    return f"{lib.norm(agent)}@{lib.canon_project(project)}" if project else lib.norm(agent)


def secret_command() -> list:
    """The command (a list of words) that prints the value of a 1Password reference given as its last argument."""
    custom = os.environ.get("POSTBOX_SECRET_CMD", "").strip()
    if custom:
        return shlex.split(custom)
    cached = Path.home() / ".claude" / "bin" / "op-cached"
    return [str(cached)] if cached.is_file() else ["op", "read"]


def op_cached(ref: str) -> str:
    """The value behind a 1Password reference (see the module docstring for which command resolves it). Raises SecretUnavailable; the reference and output are never put in the error."""
    try:
        r = subprocess.run([*secret_command(), ref], capture_output=True, text=True, timeout=RESOLVE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretUnavailable(type(exc).__name__) from None
    value = r.stdout.strip()
    if r.returncode != 0 or not value:
        raise SecretUnavailable(f"secret command exit {r.returncode}")
    return value


class _V4Connection(http.client.HTTPConnection):
    """An HTTP connection that resolves and connects over IPv4 only (the first answer on a LAN can be a useless fe80:: link-local address)."""

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)
        if not infos:
            raise socket.gaierror("no IPv4 address")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(infos[0][4])
            if getattr(self, "_use_tls", False):
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _connection(url: str, timeout: float) -> tuple[_V4Connection, str]:
    """(connection, path) for a webhook URL. https always; plain http only to this Mac itself (tests and local relays)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    loopback = host in ("localhost", "127.0.0.1")
    if parts.scheme not in ("https", "http") or not host or (parts.scheme == "http" and not loopback):
        raise ValueError("bad_url")
    conn = _V4Connection(host, parts.port or (443 if parts.scheme == "https" else 80), timeout=timeout)
    conn._use_tls = parts.scheme == "https"
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return conn, path


def reason_of(exc: BaseException) -> str:
    """A short class for a failed call. Exception TEXT is never used: it can contain the URL."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, ValueError):
        return str(exc) if str(exc) in ("bad_url", "bad_key") else "bad_request"
    if isinstance(exc, OSError):
        return "network"
    return "error_" + type(exc).__name__


class Waker:
    """The state of the wake doorbell for one process: the resolved secrets (kept after the first success) and the failure bookkeeping."""

    def __init__(self, resolver: Optional[Callable[[str], str]] = None, config: Optional[Callable[[], dict]] = None,
                 log: Optional[Callable[..., None]] = None, clock: Callable[[], float] = time.time):
        self.resolver = resolver or op_cached
        self.config = config or load_wake
        self.log = log or (lambda event, **kv: None)
        self.clock = clock
        self.secrets: dict = {}          # reference -> value, kept for the life of the process
        self.failed_at: dict = {}        # reference -> when its lookup last failed
        self.logged_failure: set = set() # references whose failure is already in the log (cleared by the next success)

    def entry(self, agent: str, project: Optional[str] = None) -> Optional[dict]:
        """The wake entry for agent@project when it has its own (a Desk's webhook), else the agent's entry (the general entry), else None."""
        config = self.config()
        if project and wake_key(agent, project) in config:
            return config[wake_key(agent, project)]
        return config.get(wake_key(agent))

    def has_own_entry(self, agent: str, project: str) -> bool:
        return wake_key(agent, project) in self.config()

    def configured(self, agent: str, project: Optional[str] = None) -> bool:
        return self.entry(agent, project) is not None

    def secret(self, agent: str, ref: str) -> str:
        if ref in self.secrets:
            return self.secrets[ref]
        now = self.clock()
        if now - self.failed_at.get(ref, -RESOLVE_RETRY_S) < RESOLVE_RETRY_S:
            raise SecretUnavailable("recently failed")
        try:
            value = self.resolver(ref)
        except Exception as exc:  # noqa: BLE001 - any resolver failure is the same answer
            self.failed_at[ref] = now
            if ref not in self.logged_failure:   # once per run of failures, not once per delivery
                self.logged_failure.add(ref)
                self.log("wake_failed", agent=agent, reason="secret_unavailable", detail=type(exc).__name__)
            raise SecretUnavailable("lookup failed") from None
        if not value:
            self.failed_at[ref] = now
            raise SecretUnavailable("empty")
        self.secrets[ref] = value
        self.failed_at.pop(ref, None)
        self.logged_failure.discard(ref)
        return value

    def wake(self, agent: str, project: str, from_addr: str, waiting: int, event: str = "mail") -> Optional[WakeResult]:
        """One attempt at the agent's webhook. None when the agent has no wake entry. Never raises; writes wake_sent / wake_failed to the log."""
        entry = self.entry(agent, project)
        if entry is None:
            return None
        try:
            timeout = min(MAX_TIMEOUT_S, max(1.0, float(entry.get("timeout_s", DEFAULT_TIMEOUT_S))))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        try:
            url = self.secret(agent, entry["url_ref"])
            header_line = self.secret(agent, entry["header_ref"]) if str(entry.get("header_ref", "")).startswith("op://") else None
            key = None if header_line else self.secret(agent, entry["key_ref"])
        except SecretUnavailable:
            return WakeResult(False, "secret_unavailable")
        sender_agent, _, rest = str(from_addr).partition("@")     # "claude@project-a[@machine]" -> agent, project (so a receiver need not parse the address)
        sender_project = lib.norm(rest.partition("@")[0]) if rest else ""
        desks = entry.get("desks") if isinstance(entry.get("desks"), dict) else {}   # optional {project: "Desk name"} in wake.json (not a secret)
        desk = desks.get(sender_project)
        body = json.dumps({"event": event, "to": f"{lib.norm(agent)}@{lib.norm(project)}", "from": from_addr, "from_agent": lib.norm(sender_agent),
                           "from_project": sender_project, "desk": desk if isinstance(desk, str) and desk else None, "inbox_count": waiting,
                           "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.clock()))}).encode()
        if header_line:   # the Desk's routine gave a whole header line ("Name: value", for example "Authorization: Bearer ..."): send exactly that
            name, _, value = header_line.partition(":")
            name, value = name.strip(), value.strip()
            if not re.fullmatch(r"[A-Za-z0-9-]+", name) or not value or name.lower() in ("content-type", "content-length", "host"):
                result = WakeResult(False, "bad_header")
                if event == "mail":
                    self.log("wake_failed", agent=agent, project=lib.norm(project), reason=result.reason)
                return result
            headers = {"Content-Type": "application/json", name: value}
        else:
            headers = {"Content-Type": "application/json", KEY_HEADER: key, "Authorization": "Bearer " + key}
        try:
            conn, path = _connection(url, timeout)
            try:
                # Both forms: Grok Bot desktop's routine shows an `Authorization: Bearer <key>` header (a typical routine
                # setting); X-Postbox-Key stays for receivers that read the plain key header.
                conn.request("POST", path, body, headers)
                response = conn.getresponse()
                response.read(1024)
                status = response.status
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            result = WakeResult(False, reason_of(exc))
        else:
            result = WakeResult(200 <= status < 300, "" if 200 <= status < 300 else f"http_{status}", status)
        if event == "mail":
            if result.ok:
                self.log("wake_sent", agent=agent, project=lib.norm(project), status=result.status, via="desk" if self.has_own_entry(agent, project) else "agent", desk=desk if isinstance(desk, str) and desk else None)
            else:
                self.log("wake_failed", agent=agent, project=lib.norm(project), reason=result.reason)
        return result
