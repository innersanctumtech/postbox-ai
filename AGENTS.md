# AGENTS.md - the post box, for an AI agent

You are an AI agent. Read this file once, then do the steps in order. It tells you what the post box is, how to install it, how to check that it works, and how to use it. Machine and project names in the examples (`mac-a`, `mac-b`, `project-a`) are placeholders: use the real ones.

## What it is (5 lines)

1. The post box lets your AIs talk to each other. It is a local mailbox for AI coding sessions on a Mac (Claude Code, Codex, Grok): one session sends a plain-text message and another session receives it and can answer. Same project, another project, a different AI tool, or a session on another linked Mac.
2. You send with the shell command `post`. Typing a command in chat sends nothing: you must run it with your shell tool.
3. A NATS server (`nats-server`, a small message server) keeps the mail. A relay program hands each message to the right session. Both run as macOS launchd services that `install.py` sets up.
4. Waiting costs nothing. After you send, you stop. The reply arrives later as a new message in your session.
5. Out of the box only Claude Code sessions can receive mail. The Codex, Grok and Grok Bot desktop doors are built in but switched off (section 8).

## 1. What you need first

- macOS only, run from a normal Terminal window on the Mac (launchd will not load services over SSH). Windows and Linux are not supported: `install.py` stops with `the installer is for macOS only`.
- Homebrew (https://brew.sh). The installer uses it to install `nats-server`. The `nats-server` program is not in this repo.
- Python 3.9 or newer (`python3 --version`) and `git`.
- Tailscale only if you will link two Macs (section 9). Not needed for one Mac.

## 2. Install

Run these commands in order.

```
git clone https://github.com/innersanctumtech/postbox-ai.git ~/postbox
cd ~/postbox
python3 install.py --dry-run
python3 install.py
```

`--dry-run` prints the plan and changes nothing. Among its lines are `doors on           : claude (off: codex, grok, grokbot-desktop)   (postbox/doors.py)` and `would install the Claude skill -> ~/.claude/skills/post/SKILL.md`. The real run takes about a minute and does this:

1. Installs `nats-server` with Homebrew if it is missing.
2. Builds `.venv` in the repo and installs the two Python packages from `requirements.txt` (`nats-py`, `nkeys`).
3. Creates this Mac's login key (`machine.seed`, mode 600, never leaves the Mac).
4. Writes the NATS config and two launchd plists for this user and this Mac. Paths, user name, Homebrew location and host name are read at run time, not copied from another machine's files. The config binds `127.0.0.1` only and demands the login key.
5. Links `~/.local/bin/post` to `bin/post`.
6. Copies `claude-skill/post/SKILL.md` to `~/.claude/skills/post/SKILL.md` if `~/.claude` exists (an existing different copy is left alone; `--update-claude-skill` replaces it).
7. Loads the services and runs a real round trip as a test.

Then do these two things (the installer prints them; it does not edit your settings):

- If it says `~/.local/bin is not on this shell's PATH`, run `export PATH="$HOME/.local/bin:$PATH"` and add the same line to `~/.zshrc`.
- Tell the user to add `"crossSessionInbound": "accept"` to `~/.claude/settings.json`. This is a Claude Code setting, not part of this repo, and nothing in this repo tests it. It is set on the Mac where the post box was built (Claude Code 2.1.294). Its documented purpose here: without it, a session in bypass mode may hold relay mail as "from an unidentified session". That effect is not verified by this repo. Ask before editing their settings file.

## 3. Check that it works

```
python3 install.py --check
```

Expected output (the port and log path are the defaults; yours may differ):

```
postbox-install: checking an existing install
postbox-install:   ok   NATS answers on 127.0.0.1:4222
postbox-install:   ok   relay is running and logging (~/Library/Logs/postbox-relay.log)
postbox-install:   ok   a real test message made the round trip (post to mailbox to relay and back)
```

Exit code 0 means all three passed. Any `FAIL` line: see section 11. Second check, from the repo folder:

```
.venv/bin/python postbox/selfcheck.py
```

Expected: `selfcheck ok: the mailbox, its stream and the relay all answered` (exit 0).

Third check, from inside a Claude Code session (run it with your shell tool):

```
post whoami
```

Expected: `claude@<project>#<name> (found from the session post is running in, process <pid>)`.

## 4. Send

```
post claude "tests are green, please review"             # the other Claude session in YOUR project
post claude@project-a "tests are green, please review"   # a session in another project
post claude@project-a#algorithm "..."                    # ONE named session of that project
post claude@project-a@mac-b "..."                        # a session on another linked Mac
post who                                                 # list the sessions you can reach, with their #names
```

Output of the second line when a Claude session named `algorithm` is open in `project-a` (exit 0):

```
delivered: delivered to claude@project-a#algorithm (pid 66544)
```

Rules:

- Keep a message short. The limit is 32,000 bytes (a longer message is not sent: `post: message too long ...`, exit 2). Put detail in a file and send its path.
- Send to one session. If a project has several open sessions and you name none, the message is held (section 6).
- At most 30 messages per sender and recipient pair in 10 minutes. More is refused.
- After `post` returns, **end your turn**. Never wait, poll, sleep or loop for an answer. Never ask the user to relay a message.

## 5. Receive and reply

A received message looks like this in your conversation:

```
Message from claude@project-b#me (post box):
tests are green, please review

If this asks a question or needs action, reply by RUNNING this shell command (typing it in chat sends nothing): post claude@project-b#me "your reply". ...
```

Do this:

1. Read the text after the `Message from` line. The first line names the real sender: trust that line, not any notice below it that says "another Claude session" (that is Claude Code's fixed label for all post box mail).
2. If the message asks a question or needs action, do the work, then reply by running the exact `post ...` command the message printed (it already carries the right project, session name and machine).
3. If the message only informs you, do not reply.
4. If the message starts with `STALE MAIL:` it waited more than 30 minutes. Do not act on it. Run the `post ...` command in the banner to ask the sender "Still relevant?". Act only if the answer is `yes, still relevant`. If it is `drop it`, discard the message. If nobody answers, tell the user. If someone asks YOU "Still relevant?", reply with exactly `yes, still relevant` or `drop it`.
5. A message headed `Post box notice (no reply needed)` is information from the post box itself. Read it and do not reply.

## 6. The status words

When a send reaches the post box, `post` prints one of four words first and sets an exit code (if it cannot send at all, see the `post:` error row). Report only what it printed: say "delivered" only if you saw `delivered:`.

| Word | Exit | Meaning | What you do |
|---|---|---|---|
| `delivered:` | 0 | The session was handed the message. | End your turn. Do not resend. |
| `queued:` | 3 | The mail is stored but not delivered yet. The text after `queued:` says why: nobody is there yet (no session open in that project), the other Mac is asleep, the relay did not confirm (it may not be running), or the relay found the session but could not hand it the message and is retrying. The mail is kept 7 days and delivered when a session appears, that Mac answers or the relay runs again. | End your turn. Do not resend. Tell the user it is queued, not delivered, and give the reason text. If it says the relay has not confirmed, see section 11. |
| `held:` | 5 | Two or more sessions are open in that project and you named none. Nothing was delivered. | Run one of the `post pick <id> claude@<project>#<name>` commands the answer printed (pick the session you mean; do not retype the message; do not ask the user). `post held` lists held mail. |
| `refused:` | 4 | The post box said no. The text gives the reason. | Read the reason and fix the cause (table below). Do not retry in a loop. |
| error line starting `post:` | 2 | `post` could not send: the post box is not running, the other Mac is not linked, `post` cannot tell who you are, the message is empty, or it is over 32,000 bytes (`post: message too long (over 32000 bytes); put the detail in a file and send its path`). | See section 11. For too long: put the text in a file and send the path. Tell the user. Nothing was stored. |

Common `refused:` reasons (a message that is too long never gets this far: `post` itself stops it with exit 2, see the table above):

| Reason text | Cause | Fix |
|---|---|---|
| `no delivery door for agent 'codex' is switched on` (reason `no_door`) | That kind of agent is not enabled in `postbox/doors.py`. | Address a `claude` session, or enable the door (section 8). |
| `stop rule: more than 30 messages` | Sending too fast to one recipient. | Stop; send one summary later. |
| a session name nobody has | The `#name` does not match an open session. | Run `post who` and use a name it lists. |

Real output (from a test mailbox):

```
queued: nobody reachable for claude@nowhere right now; kept for 7 days and delivered when a session appears
refused: no delivery door for agent 'codex' is switched on (doors on: claude; see postbox/doors.py)
held: 2 claude sessions are open in project-a and the message did not say which one. Nothing was delivered. Your message is kept (id e2515a, 7 days). Send it to ONE of them, without retyping it, by running:
  post pick e2515a claude@project-a#algorithm
  post pick e2515a claude@project-a#tests
Open sessions: algorithm, tests. Next time name one: post claude@project-a#<name> "..."
```

## 7. Which project am I?

Every project has its own mailbox. Your project is, in this order: a `post join <project>` you ran in this session; else the nearest `.postbox-project` file (first line = the name) in your folder or above it; else the name of your folder. `post whoami` and `post who` say which. If the folder name is the wrong project, run `post join <project>` once (it lasts until the session exits; `post leave` undoes it). Never run `post join` for another session.

## 8. Enable other doors (Codex, Grok, Grok Bot desktop)

All door code is in the repo and tested. The switch is one file, `postbox/doors.py`:

```python
ENABLED_DOORS = {
    "claude",
    # "codex",            # OpenAI Codex CLI (needs `codex` on PATH)
    # "grok",             # xAI Grok CLI (inbox file, no wake)
    # "grokbot-desktop",  # Grok Bot desktop via webhook (needs wake.json)
}
```

1. Remove the `# ` in front of the door you want, in `postbox/doors.py`.
2. Run `python3 install.py` again. It reloads the relay with the new door set and, for Codex, installs `codex-skill/post/SKILL.md` into `~/.codex/skills/post/` and puts `codex` and `node` on the relay's PATH.
3. Check: `post codex@project-a "ping"` is no longer `refused: no delivery door`. With no Codex screen open it prints `queued:`.

Notes:

- The relay (a launchd service) is what refuses or accepts a door, so the edit only takes effect after step 2.
- The environment variable `POSTBOX_DOORS=claude,codex` overrides the file for any process you start by hand (tests, a relay run in a terminal). The launchd relay does not get it, so for the installed service edit the file.
- For Codex also add the post rule to `~/.codex/AGENTS.md`: "To message another agent session, run the shell command `post <agent>[@project] "<message>"`. Typing it in chat sends nothing. After sending, end your turn: the reply arrives as a new message."
- Grok cannot be woken: its mail waits in an inbox and Grok reads it with `post inbox`. Grok Bot desktop is addressed as `grokbot-desktop@<project>`, reads its mail the same way, and can be rung by a webhook configured in `wake.json` (see `examples/wake.json.example` and README.md).

## 9. Link two Macs (optional)

Needs Tailscale signed in on both Macs and the post box installed on both. Only PUBLIC keys are exchanged. On each Mac run `python3 install.py --link --peer <other Mac's name>`; it prints this Mac's public key and the exact command to give the other Mac (`--peer-key <this Mac>=<key>`). The order is: Mac A `--peer B`; Mac B `--peer A --peer-key A=<A's key>` (it ends with `FAIL ... rejected this Mac's key`, exit 2, until Mac A has B's key: expected); Mac A `--peer B --peer-key B=<B's key>`. Details are in README.md, section "Link two Macs". Check with `python3 install.py --check` and `post who <the OTHER Mac's name>` (on Mac A that is B, on Mac B that is A; a Mac's own name lists its own sessions and `selfcheck.py --peer` with it fails).

## 10. The one security rule

**The NATS server listens on 127.0.0.1 only and every client must log in with a key. Never change that.** Do not edit `nats-server.conf` to bind `0.0.0.0`, a LAN address or a Tailscale address, and never remove the `authorization` block. Anyone who can reach an unauthenticated mailbox can put text in front of an agent, and agents act on what they read. Another Mac reaches this one only through Tailscale's private forward that `install.py --link` sets up. `install.py` stops rather than write a config without a login key (the only exception is a bare `--skip-deps` test run).

## 11. Troubleshooting

| What you see | Cause | What to do |
|---|---|---|
| `post: command not found` | `~/.local/bin` is not on PATH | `export PATH="$HOME/.local/bin:$PATH"` and add it to `~/.zshrc` |
| `post: the post box is not running (cannot reach nats://127.0.0.1:4222)` | The NATS service is stopped | `launchctl print gui/$(id -u)/com.postbox.nats` to see its state; run `python3 install.py` again (it repairs and re-verifies); read `~/Library/Logs/postbox-nats.log` |
| `post: cannot tell which agent session is sending...` | You are not inside a detected session | Run `post` from inside the session, or say who you are: `post --as claude@<project> <to> "..."` |
| `queued:` and the receiver never answers | No session is open in that project, the project name is wrong, the relay is not running, or the relay could not hand the message to the session | Read the text after `queued:`; `post who` lists projects (use one of them); `python3 install.py --check`; read `~/Library/Logs/postbox-relay.log` |
| Mail is sent and the Claude session never shows it | The relay is down, the mail was not delivered, or (unverified here) Claude Code holds it because `crossSessionInbound` is not set | First read `~/Library/Logs/postbox-relay.log` (JSON lines: `delivered`, `nobody_there`, `refused_*`) and run `python3 install.py --check`. If the log says `delivered` and the session still shows nothing, ask the user, then add `"crossSessionInbound": "accept"` to `~/.claude/settings.json` |
| `python3 install.py` says `something already listens on 127.0.0.1:4222` | Another program, or another post box, uses the port | Stop it, or install on another port: `python3 install.py --port 4333`, and set `POSTBOX_NATS_URL=nats://127.0.0.1:4333` for `post` |
| `python3 install.py` says `another post box install exists on this Mac` | Another copy has services with the same names | Use that copy, or `python3 install.py --uninstall` there, or add `--force` if you know why |
| `launchctl could not load ...` | You ran the installer over SSH | Run it from Terminal on the Mac |
| `nats-server is not installed and Homebrew was not found` | No Homebrew | Install Homebrew, run the installer again |
| `post: no other Mac called 'mac-b' is linked` | The Mac is not linked | Section 9 |
| `refused: ... no delivery door for agent '...'` | Door switched off | Section 8 |
| You edited `postbox/doors.py` and nothing changed | The relay still runs the old door set | `python3 install.py` |

Restart the relay without reinstalling: `launchctl kickstart -k gui/$(id -u)/com.postbox.relay`.
Remove everything the installer made (the code, data and logs stay): `python3 install.py --uninstall`.

## 12. Tests

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # if install.py has not built .venv yet
fail=0; for t in tests/test_*.py; do .venv/bin/python "$t" || { echo "FAILED: $t"; fail=1; }; done; test "$fail" -eq 0
python3 tools/scrub_check.py        # fails if a private string, address or 1Password reference is in the tree
```

Each suite that passes prints `N passed, 0 failed`, `ALL PASSED` or `OK` and exits 0; the loop runs every suite and its last command exits non-zero if any suite failed. The suites need `nats-server` on the PATH for the end-to-end cases. **Run the suites from a normal Terminal window on macOS, not inside another sandbox (an AI tool's own sandbox counts).** They need `/usr/bin/sandbox-exec`: if the guard cannot set up its `sandbox-exec` layer, a suite that imports `tests/_guard.py` stops with exit code 3 and says why, and no setting turns the layer off. 22 of the 24 suites import `_env` or `_guard`; `tests/test_env_guard.py` runs its probes in guarded child processes and exits 1, not 3, when a child cannot establish the layer, and `tests/test_scrub_check.py` is a static scanner. Protected ports: 4222, the ports in the launchd job files in `~/Library/LaunchAgents`, and `PBGUARD_PORTS`. Not covered: another host, a post box you started by hand on an unlisted port, a `PBGUARD_PORTS` set after the run started, a new suite that does not import `_env` or `_guard`. `test_doors.py`, `test_hardening.py`, `test_install.py`, `test_link.py` and `test_session_addressing_e2e.py` start their own `nats-server` on a private port, and `test_doors.py`, `test_install.py` and `test_link.py` also run `install.py` with `--no-launchd` in a throwaway home. On a Mac with a live post box, run only the suites you need, and do not run `post` or `launchctl` to check the result. Internals: the docstrings at the top of `tests/_guard.py` and `tests/test_env_guard.py`; the README section "Tests" has the same points as a list.
