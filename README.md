# Post box

**Let your AIs talk to each other.**

You run Claude Code, Codex and Grok in separate windows, and you carry results between them by hand. The post box removes you from that loop. One AI session runs `post codex "review my change"`; the other session wakes up with that message in its conversation, does the work, and answers with the exact command that was attached to the message. It works between sessions in one project, between projects, between different AI tools, and between two linked Macs. Waiting costs nothing: no AI runs until a message arrives.

**If you are an AI agent, read [AGENTS.md](AGENTS.md).** It has the install commands, the check, the status words and the troubleshooting table in the order you need them. Claude Code readers: [CLAUDE.md](CLAUDE.md) points there.

What you get:

- **One command between AI tools.** Claude Code, Codex, Grok and Grok Bot desktop each have a built-in "door" (how the relay hands a message to that kind of agent). In this release only the Claude Code door is switched on; the others are built in, tested, and switched on by one file ([Other doors](#other-doors)). Until you switch a door on, mail for that tool is `refused` with the reason `no_door`.
- **Across your Macs.** Mail for a linked Mac that is asleep is stored on the sender's Mac and handed over when the other Mac answers.
- **Honest results.** A send that reaches the post box ends in one of four words: `delivered`, `queued` (stored, delivery not confirmed yet; kept 7 days), `held` (several sessions are open and you named none) or `refused` (with the reason). If `post` cannot send at all it prints a `post:` error line and exits 2.
- **Safe by default.** The mailbox listens on `127.0.0.1` only, clients log in with a key (see "Security model"), senders are rate limited, messages are capped at 32,000 bytes, and mail older than 30 minutes arrives with a "do not act yet, ask the sender" banner.
- **Built for an AI to install.** Point any AI at [AGENTS.md](AGENTS.md) and it has what it needs.

## How it works

```
post (command)  ->  NATS JetStream (the mailbox)  ->  relay  ->  door  ->  the agent session
```

- `post` (`bin/post`, `postbox/post.py`) puts a message in the mailbox and waits a moment for the relay's answer.
- The mailbox is a [NATS](https://nats.io) server with JetStream (durable storage). The `nats-server` program is not in this repo: `install.py` installs it with Homebrew.
- The relay (`postbox/relay.py`) takes each message, picks the door for the addressed agent and delivers. For Claude Code the door is the session's own Unix socket.
- Both servers run as launchd services (`com.postbox.nats`, `com.postbox.relay`), started at login and restarted if they stop.

### How NATS is packaged

`install.py` is the packaging. It does not copy a fixed config. On each Mac it:

1. finds or installs `nats-server` (`brew install nats-server`; or set `NATS_SERVER_BIN`),
2. builds `.venv` and installs `nats-py` and `nkeys` from `requirements.txt`,
3. generates this Mac's login key and writes `nats-server.conf` and the two launchd plists into the current user's `~/Library` folders,
4. fills in paths at run time from the user's home folder, the repo folder and the host name; the data folder is `<repo>/jetstream` unless `POSTBOX_STORE_DIR` says otherwise,
5. loads the services and proves the result with a real test message.

The shipped files are checked by `tools/scrub_check.py` for machine-specific strings (home paths, private addresses, and your own denylist); the one thing the installer adds to the repo folder is the public key files under `peers/` when you link Macs (see "Link two Macs"). Moved the repo or changed the user? Run `python3 install.py` again; it rewrites the files and repairs the rest.

## Requirements

- macOS only, with a normal Terminal window (launchd will not load services over SSH). Windows and Linux are not supported: `install.py` stops with `the installer is for macOS only`.
- [Homebrew](https://brew.sh) (for `nats-server`).
- Python 3.9 or newer, and `git`.
- Tailscale, only if you link two Macs.

## Install

```
git clone https://github.com/innersanctumtech/postbox-ai.git ~/postbox
cd ~/postbox
python3 install.py --dry-run     # prints the plan, changes nothing
python3 install.py               # installs or repairs, then verifies (about a minute)
python3 install.py --check       # verifies an existing install any time
```

`--check` prints one line per check and exits 0 when all pass:

```
postbox-install: checking an existing install
postbox-install:   ok   NATS answers on 127.0.0.1:4222
postbox-install:   ok   relay is running and logging (~/Library/Logs/postbox-relay.log)
postbox-install:   ok   a real test message made the round trip (post to mailbox to relay and back)
```

The installer stops without changing anything if another post box is already installed or something else listens on the port (`--force` overrides, `--port N` picks another port). `python3 install.py --uninstall` removes the services and the `post` link; the code, the mailbox data and the logs stay.

Two things it prints but leaves to you:

1. If `~/.local/bin` is not on your PATH: `export PATH="$HOME/.local/bin:$PATH"` (and put that line in `~/.zshrc`).
2. Claude Code: add `"crossSessionInbound": "accept"` to `~/.claude/settings.json`. This is a Claude Code setting, not part of this repo, and nothing in this repo tests it. It is set on the Mac where the post box was built (Claude Code 2.1.294). Its documented purpose here: without it, a session in bypass mode may hold relay mail as "from an unidentified session". That effect is not verified by this repo. If mail never shows up in a session, read the relay log first (`~/Library/Logs/postbox-relay.log`: `delivered` means the relay handed the mail over), then check this setting.

The installer also copies `claude-skill/post/SKILL.md` to `~/.claude/skills/post/SKILL.md` (if `~/.claude` exists), which teaches Claude Code sessions to use `post`.

`pip install .` installs only the `post` command (into the active environment). The mailbox and relay services come from `install.py`.

## Use

```
post claude "build is green"                  # the other Claude session in YOUR project
post claude@project-a "build is green"        # a session in another project
post claude@project-a#algorithm "..."         # ONE named session of that project
post claude@project-a@mac-b "..."             # a session on another linked Mac
post claude@@mac-b "..."                      # my own project, on that Mac
post who                                      # sessions you can reach now, with their #names
post who mac-b                                # the same on another linked Mac
post whoami                                   # who post thinks you are, and how it found out
post held                                     # messages held because they did not name a session
post pick <id> claude@project-a#algorithm     # send a held message on, without retyping it
post join <project>                           # make THIS session belong to <project> (post leave undoes it)
post --as claude@myproject <to> "..."         # state who you are when post cannot tell
```

A receiving session sees:

```
Message from claude@project-b#me (post box):
tests are green, please review

If this asks a question or needs action, reply by RUNNING this shell command (typing it in chat sends nothing): post claude@project-b#me "your reply". ...
```

After sending, the sender ends its turn. The reply arrives later as a new message.

### Results

| Word | Exit | Meaning |
|---|---|---|
| `delivered` | 0 | The session was handed the message. |
| `queued` | 3 | The message is stored but not delivered yet: nobody is there yet, the other Mac is asleep, the relay did not answer, or the relay found the session but could not hand it the message and is retrying. The text after `queued:` says which. Kept 7 days (`POSTBOX_MAX_AGE_S`), delivered when a session appears, that Mac answers or the relay runs again. |
| `refused` | 4 | The relay said no; the text gives the reason (door off, stop rule, unknown session name). |
| `held` | 5 | Two or more sessions are open in that project and the message named none. Nothing was delivered. The answer lists one `post pick <id> <address>#<name>` command per session. |
| error line `post: ...` | 2 | `post` could not send: the post box is not running, the other Mac is not linked, `post` cannot tell who is sending, the message is empty, or it is over 32,000 bytes (`post: message too long ...`; put the detail in a file and send its path). Nothing was stored. |

### Projects and sessions

Every project has its own mailbox. A session's project is, in order: a `post join <project>` it ran, else the nearest `.postbox-project` file (first line = the name) in its folder or above it (never read in your home folder), else its folder name. Several sessions can be open in one project: a message goes to ONE session, never to all. Sessions are addressed as `agent@project#name`, where the name is the one `post who` shows (Claude Code's `/rename` name, or its automatic one). Mail for a project with two open sessions and no `#name` is held on the receiving Mac for 7 days (at most 200 messages, `POSTBOX_HOLD_MAX`).

`aliases.json` in the support folder (example: `examples/aliases.json.example`) makes two project names one project.

### Stale mail

Mail that waited more than 30 minutes before reaching a session (`POSTBOX_STALE_AFTER_S`) starts with `STALE MAIL:`. The receiver does not act on it; it runs the command in the banner to ask the sender "Still relevant?" and acts only on `yes, still relevant`.

## Other doors

The switch is `postbox/doors.py`:

```python
ENABLED_DOORS = {
    "claude",
    # "codex",            # OpenAI Codex CLI (needs `codex` on PATH)
    # "grok",             # xAI Grok CLI (inbox file, no wake)
    # "grokbot-desktop",  # Grok Bot desktop via webhook (needs wake.json)
}
```

1. Remove the `# ` in front of a door.
2. Run `python3 install.py` again. It reloads the relay with the new door set, installs that platform's skill (Codex: `codex-skill/post/SKILL.md` into `~/.codex/skills/post/`) and puts `codex` and `node` on the relay's PATH.

Mail for an agent whose door is off is refused with the reason `no_door`. The environment variable `POSTBOX_DOORS=claude,codex` replaces the file for a process started by hand (the tests use it); the launchd relay does not get it, so edit the file for the installed service.

- **Codex** is found from its process list and its thread database (read-only). A Codex screen reads mail only between tasks, so a send can end with `Heads up: ... is mid-task`. Add the post rule to `~/.codex/AGENTS.md`: "To message another agent session, run the shell command `post <agent>[@project] "<message>"`. Typing it in chat sends nothing. After sending, end your turn: the reply arrives as a new message." The relay checks that each Codex delivery really reached the conversation and logs `codex_seen` or `codex_unseen`; set `POSTBOX_ALARM_CMD` to a script to be run on `codex_unseen`.
- **Grok** (the xAI command line tool) is recognised from `~/.grok/active_sessions.json` and cannot be woken. Its mail waits in an inbox folder (50 messages, 7 days) and the session runs `post inbox` at the start of each turn. `post inbox --watch` prints one short line when mail arrives; `post inbox --history` shows what was read in the last 7 days.
- **Grok Bot desktop** is a different app from the Grok command line tool. Its agent name is `grokbot-desktop`, it is known only by stating who it is (`post --as grokbot-desktop@project-a ...`), and it reads its inbox the same way. Optionally the relay rings a wake webhook after each delivery. Configure it in `wake.json` in the support folder (example: `examples/wake.json.example`). The file holds 1Password references like `op://<vault>/<item>/<field>`, never secrets; they are resolved at call time by `$POSTBOX_SECRET_CMD`, else `~/.claude/bin/op-cached` if it exists, else `op read`. `post wake-test grokbot-desktop@project-a` rings it once and stores nothing. A project with its own Desk entry (`grokbot-desktop@project-a`) gets its own webhook; mail sent to the general address from such a project is redirected to its Desk unless the sender is listed in `desk_sender_projects` or `"desk_redirect": false` is set.

## Link two Macs

Needs Tailscale signed in on both Macs and the post box installed on both. Every Mac has its own login key pair; only the PUBLIC halves (safe to share) are exchanged. Say the Macs are called `mac-a` and `mac-b` (`--machine NAME` sets a name; the default is the host name).

1. On `mac-a`: `python3 install.py --link --peer mac-b`. It makes this Mac's key, forwards `mac-a`'s tailnet port to the mailbox with `tailscale serve` (private to your tailnet, never the internet), and prints `PUBLIC KEY (safe to share) of this Mac, mac-a: <key>` plus the exact command to run on the other Mac. `WARN mac-b did not answer` is normal at this point.
2. On `mac-b`: `python3 install.py --link --peer mac-a --peer-key mac-a=<the public key from step 1>`. It prints `mac-b`'s own public key. Its last check, a real message through `mac-a`, ends with `FAIL mac-a answered but rejected this Mac's key` and exit code 2: that is expected, because `mac-a` does not know `mac-b`'s key yet. The key was printed before the error.
3. Back on `mac-a`: `python3 install.py --link --peer mac-b --peer-key mac-b=<the public key from step 2>`. Now both Macs trust each other.
4. Check on `mac-a`: `python3 install.py --check` (it also tests each linked Mac), `post who mac-b`, `.venv/bin/python postbox/selfcheck.py --peer mac-b`. On `mac-b` give the OTHER Mac's name: `post who mac-a`, `.venv/bin/python postbox/selfcheck.py --peer mac-a`. A Mac is never its own peer: `--peer` with its own name fails (`install.py` says `--peer NAME is this Mac itself`; `post who` with its own name lists its own sessions instead).

The installer saves keys as `peers/<machine>.pub` in the repo folder. Do not commit them to a public fork: give the other Mac the key with the command the installer prints. The option `--publish-key` runs `git add`, `commit` and `push` of that file for you; use it only when the repo's remote is private. The tailnet name of the other Mac is looked up in Tailscale at run time (`--peer NAME=HOST` or `POSTBOX_PEER_<NAME>=host[:port]` override). `--uninstall` removes the Tailscale forward it made and never touches one it did not make. A lost or replaced key: delete `machine.seed` in the support folder, run `install.py --link ...` again and re-run `--link` on the other Mac. This procedure is covered by `tests/test_link.py` with a stand-in for Tailscale and two emulated Macs; it needs a real tailnet for a real run.

## Security model

- **NATS listens on `127.0.0.1` only.** No LAN address, no Tailscale address. Do not change that. Another Mac arrives through Tailscale's private forward, not by a wider bind.
- **Every client logs in.** The config demands an nkey (ed25519, challenge-response) from every process, this Mac's own included, so a program without a key cannot put text in front of an agent. The key file (`machine.seed`, mode 600) is readable by your own user, so this keeps out other users and unkeyed programs, not other programs running as you. The installer refuses to write a login-less config except for a bare `--skip-deps` test setup.
- **A linked Mac's key is limited.** The config (`peer_permissions` in `install.py`) lets it publish on `post.>` and `who.>` and subscribe to its own inbox prefix, and grants it nothing else, so it has no access to the stream's API or to other inboxes.
- **Hostile input is cleaned.** Sender addresses, project names and session names are reduced to lower case letters, digits and hyphens; reply inboxes must be real `_INBOX` names; a message over 32,000 bytes is refused; each sender pair is limited to 30 messages per 10 minutes and each recipient to 120.
- **Sender discovery is not authentication.** Anyone who can run `post` on a Mac can already name themselves with `--as`; that is the local trust boundary, the same as for any local session.
- **Telemetry is off** unless you set `POSTBOX_TELEMETRY=1`. It is built to record metadata only (see the header of `postbox/telemetry.py`), not message text.

## Files

| Where | What |
|---|---|
| `~/Library/Application Support/postbox/` | `nats-server.conf` (mode 600), `machine.seed` (this Mac's private key, mode 600, never leaves the Mac), `link.json` (linked Macs), `wake.json`, `aliases.json`, inbox and held-mail folders. `POSTBOX_SUPPORT_DIR` moves it. |
| `~/Library/LaunchAgents/com.postbox.nats.plist`, `com.postbox.relay.plist` | The two services. |
| `~/Library/Logs/postbox-nats.log`, `postbox-relay.log` | Logs. The relay log is JSON lines: `delivered`, `nobody_there`, `refused_*`, `forward_stored`, `forwarded`, `heartbeat`. |
| `<repo>/jetstream/` | Mailbox data (git-ignored). `POSTBOX_STORE_DIR` moves it. |
| `~/.local/bin/post` | Link to `bin/post`. |

Stop or start a service: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/<label>.plist` and `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`. Restart the relay: `launchctl kickstart -k gui/$(id -u)/com.postbox.relay`.

### Settings (environment variables)

| Variable | Default | Effect |
|---|---|---|
| `POSTBOX_DOORS` | the list in `postbox/doors.py` | Comma-separated doors to enable, replacing the file for that process. |
| `POSTBOX_NATS_URL` | `nats://127.0.0.1:4222` | Which mailbox `post` and the relay use. |
| `POSTBOX_MACHINE` | host name | This Mac's name in messages. |
| `POSTBOX_SUPPORT_DIR`, `POSTBOX_STORE_DIR` | see Files | Where state and mailbox data live. |
| `POSTBOX_MAX_AGE_S` | 604800 (7 days) | How long queued mail is kept. |
| `POSTBOX_STALE_AFTER_S` | 1800 | Age after which mail gets the stale banner. |
| `POSTBOX_PAIR_LIMIT`, `POSTBOX_PAIR_WINDOW_S`, `POSTBOX_RECIPIENT_LIMIT` | 30, 600, 120 | The stop rules. |
| `POSTBOX_FORWARD_RETRY_S` | 60 | How often mail kept for a sleeping Mac is retried. |
| `POSTBOX_AS` | unset | Sender identity when no session is detected (same as `--as`). |
| `POSTBOX_TELEMETRY` | `0` | `1` turns on the metadata-only event log (`POSTBOX_TELEMETRY_SPOOL` picks the folder). |
| `POSTBOX_ALARM_CMD`, `POSTBOX_SECRET_CMD` | unset | Optional helper commands (Codex "unseen" alarm; resolving `op://` references). |

## Tests

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # skip if install.py already built .venv
fail=0; for t in tests/test_*.py; do .venv/bin/python "$t" || { echo "FAILED: $t"; fail=1; }; done; test "$fail" -eq 0
```

Each suite that passes prints `N passed, 0 failed`, `ALL PASSED` or `OK` and exits 0; the loop above runs every suite and its last command exits non-zero if any suite failed. The suites work in throwaway folders, and a suite that needs a mailbox starts its own on a spare port. `tests/_env.py` turns the optional doors on for the suites that import it; `tests/test_doors.py` checks the shipped default (Claude only) end to end. `tools/scrub_check.py` is the release gate: it fails on a home folder path, private or tailnet address, real `op://` reference, login seed, or leftover `HANDOFF.md`, and with `--denylist FILE` also on your own private strings.

Test guard. `tests/_guard.py` keeps the suites away from a post box that is running on this Mac. It needs macOS with `/usr/bin/sandbox-exec`, so run the suites from a normal Terminal window, not inside another sandbox (an AI tool's own sandbox counts). If the guard cannot set up that layer, a suite that imports it stops with exit code 3 and says why; no setting turns the layer off. Internals: see the docstrings at the top of `tests/_guard.py` and `tests/test_env_guard.py`.
- Protected ports: 4222, the ports found in the launchd job files in `~/Library/LaunchAgents`, and the ports listed in `PBGUARD_PORTS` (comma separated).
- Not covered: a connection to another host; a post box you started by hand on a port that is not protected; a `PBGUARD_PORTS` value set after the test run has started; a new suite that does not import `_env` or `_guard`.
- 22 of the 24 suites in `tests/` import `_env` or `_guard`. The other two: `tests/test_env_guard.py` runs its probes in guarded child processes and exits with code 1, not 3, when a child cannot establish the layer; `tests/test_scrub_check.py` is a static scanner. `tests/test_env_guard.py` checks that README.md, AGENTS.md and CONTRIBUTING.md state this count.
- `test_doors.py`, `test_hardening.py`, `test_install.py`, `test_link.py` and `test_session_addressing_e2e.py` start a `nats-server` of their own on a private port, and `test_doors.py`, `test_install.py` and `test_link.py` also run `install.py` with `--no-launchd` in a throwaway home. The guard does not stop suites from starting processes, so run these five only when that is acceptable.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). MIT license, see [LICENSE](LICENSE).
