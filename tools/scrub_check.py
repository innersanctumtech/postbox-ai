#!/usr/bin/env python3
"""scrub_check: fail when a tree about to be published still holds something private.

    python3 tools/scrub_check.py                         scan the repo this file lives in
    python3 tools/scrub_check.py --root DIR              scan another folder
    python3 tools/scrub_check.py --denylist FILE         also fail on every string listed in FILE (your own names, hosts, projects)
    python3 tools/scrub_check.py --skip-handoff-check    ignore a root HANDOFF.md entirely (its presence and its content), only while a release is still being prepared

Checks that need no list (they are the same for everyone):
  home-path     a macOS home folder path (the folder that holds all user accounts, followed by a user name)
  private-ip    a private LAN address (the three private ranges 10/8, 172.16/12 and the 192-prefixed /16), a link-local one (169.254/16) or a Tailscale one (100.64/10)
  tailnet-name  a machine name inside a real tailnet (<machine>.tail<hex>.ts.net)
  op-reference  a 1Password reference (op://...) that is not entirely public: the vault and item parts must be exactly <placeholders> and any later part a
                <placeholder> or a generic field word (url, key, password...), such as op://<vault>/<item>/<field>
  dated-note    a calendar date (YYYY-MM-DD) in prose or a comment: it dates a private event. A full timestamp with a T (test data) is fine
  login-seed    a NATS login seed (the private half of a key; starts with SU)
  handoff       a HANDOFF.md at the root: working notes are deleted before publishing
  junk          a .DS_Store file, a macOS "._" companion file, a Thumbs.db file, a __pycache__ folder, a .pyc, .lock, .swp or .orig file, or a name ending in ~

The denylist file holds one string per line (matched case-insensitively, as plain text, in file contents AND file names); blank lines
and lines starting with # are ignored; a line "@allow PATH" exempts that one file (relative to the root) from the denylist, for example
the license line that names the copyright holder. KEEP THE DENYLIST OUTSIDE THE REPO: a list of private strings inside a public repo
is itself a leak, which is why this tool ships without one.

Exit code 0 and "scrub gate: 0 hits" when clean; exit code 1 and one line per hit (file:line: rule) when not; exit code 2 on bad usage.
Stdlib only.
"""
import argparse
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "node_modules", "jetstream"}
JUNK_NAMES = {".DS_Store", "Thumbs.db", ".localized"}   # files a file manager or an editor leaves behind
JUNK_SUFFIXES = (".pyc", ".lock", ".swp", ".orig")
# Patterns are assembled from pieces so this file does not contain the strings it hunts for (the gate scans itself too).
HOME_PATH = re.compile("/" + "Users" + "/[A-Za-z0-9_.-]")
PRIVATE_IP = re.compile(
    r"(?<![0-9.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.\d{1,3}\.\d{1,3})(?!\.?[0-9])")
# Both ends use the same token boundary: no digit or dot-then-digit may follow (a five-part dotted number is not an address), and no digit or
# dot may precede. A dot that ends a sentence still counts as the end of the address.
TAILNET_NAME = re.compile(r"\.tail[0-9a-f]{4,}\.ts\.net", re.I)
OP_REF = re.compile("op:" + r"//([^\s\"'`\\]*)")   # the reference ends at a space, a quote, a backtick or a backslash (source code writes "\n" next to it)
OP_PLACEHOLDER = re.compile(r"<[A-Za-z][A-Za-z0-9_.-]*>")
OP_GENERIC_FIELDS = frozenset({"url", "key", "header", "field", "password", "username", "token", "secret", "credential", "notes", "value"})


class _OpReference:
    """A 1Password reference passes only when its WHOLE shape is public: the vault part and the item part must each be exactly a
    <placeholder>, and every later part (section, field) must be a <placeholder> or one of the generic field words in OP_GENERIC_FIELDS.
    A real item name, a real field name, or a real word glued to a placeholder (<item>Word) all fail. Nothing after the scheme (the bare
    scheme in prose, followed by dots, or inside a quoted string) is not a reference and passes. The tests spell out each case."""

    @staticmethod
    def private(token):
        """True when the text after the scheme names something that is not public. Prose punctuation after the reference and a ?query are ignored."""
        parts = token.split("?")[0].rstrip("\"'`)]},;:.").split("/")
        for number, part in enumerate(parts):
            public = OP_PLACEHOLDER.fullmatch(part) if number < 2 else (OP_PLACEHOLDER.fullmatch(part) or part.lower() in OP_GENERIC_FIELDS)
            if not public and (part or any(parts[number + 1:])):
                return True
        return False

    @classmethod
    def search(cls, line):
        return any(cls.private(match.group(1)) for match in OP_REF.finditer(line))


OP_REFERENCE = _OpReference()
DATED_NOTE = re.compile(r"(?<![\dT:])20\d\d-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])(?![\dT])")
LOGIN_SEED = re.compile(r"(?<![A-Za-z0-9])SU[A-Z2-7]{56}(?![A-Za-z0-9])")   # not \b: "seed_SU..." has no word boundary and must still hit
RULES = (("home-path", HOME_PATH), ("private-ip", PRIVATE_IP), ("tailnet-name", TAILNET_NAME), ("op-reference", OP_REFERENCE), ("login-seed", LOGIN_SEED), ("dated-note", DATED_NOTE))


def read_denylist(path):
    """(strings, allowed_paths) from a denylist file."""
    strings, allowed = [], set()
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@allow "):
            allowed.add(line[len("@allow "):].strip())
        else:
            strings.append(line)
    if not strings:
        raise ValueError(f"denylist {path} has no entries (only blank lines, comments or @allow lines), so it would check nothing")
    return strings, allowed


def is_text(path):
    try:
        with open(path, "rb") as fh:
            return b"\0" not in fh.read(8192)
    except OSError:
        return False


def walk(root):
    """Every file under root as a path relative to it, skipping the folders that are never published."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            yield Path(folder, name)


def scan(root, denylist=None, skip_handoff_check=False):
    """Returns a list of (relative path, line number or 0, rule, excerpt)."""
    root = Path(root).resolve()
    strings, allowed = read_denylist(denylist) if denylist else ([], set())
    lowered = [s.lower() for s in strings]
    hits = []
    if (root / "HANDOFF.md").exists() and not skip_handoff_check:
        hits.append(("HANDOFF.md", 0, "handoff", "working notes must be deleted before publishing"))
    for folder, dirs, _files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for d in dirs:
            if d == "__pycache__":
                hits.append((str(Path(folder, d).relative_to(root)), 0, "junk", "__pycache__ folder"))
    for path in walk(root):
        rel = str(path.relative_to(root))
        if rel == "HANDOFF.md" and skip_handoff_check:
            continue   # working notes of a release in preparation: they are deleted before publishing, so their content is not scanned
        if path.name in JUNK_NAMES or path.name.startswith("._") or path.name.endswith("~") or path.suffix in JUNK_SUFFIXES:
            hits.append((rel, 0, "junk", path.name))
            continue
        if "__pycache__" in path.parts:
            continue
        for number, entry in enumerate(lowered, 1):
            if entry in rel.lower() and rel not in allowed:
                hits.append((rel, 0, "denylist", f"file name matches denylist entry {number}"))
        if not is_text(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            hits.append((rel, 0, "unreadable", str(exc)))
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for rule, pattern in RULES:
                if pattern.search(line):
                    hits.append((rel, line_number, rule, line.strip()[:100]))
            if lowered and rel not in allowed:
                low = line.lower()
                for number, entry in enumerate(lowered, 1):
                    if entry in low:
                        hits.append((rel, line_number, "denylist", f"denylist entry {number}: {line.strip()[:100]}"))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fail when a tree about to be published still holds something private.")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="folder to scan (default: the repo this file lives in)")
    ap.add_argument("--denylist", help="file of private strings that must not appear (keep it outside the repo)")
    ap.add_argument("--skip-handoff-check", action="store_true", help="do not count HANDOFF.md as a hit (only while a release is being prepared)")
    args = ap.parse_args(argv)
    if not Path(args.root).is_dir():
        print(f"scrub_check: {args.root} is not a folder", file=sys.stderr)
        return 2
    if args.denylist and not Path(args.denylist).is_file():
        print(f"scrub_check: denylist {args.denylist} not found", file=sys.stderr)
        return 2
    try:
        hits = scan(args.root, args.denylist, args.skip_handoff_check)
    except (ValueError, OSError) as exc:      # an empty, unreadable or non-UTF-8 denylist is an error, never a silent "0 hits"
        print(f"scrub_check: {exc}", file=sys.stderr)
        return 2
    for rel, number, rule, excerpt in hits:
        print(f"{rel}:{number}: [{rule}] {excerpt}" if number else f"{rel}: [{rule}] {excerpt}")
    print(f"scrub gate: {len(hits)} hit{'s' if len(hits) != 1 else ''}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
