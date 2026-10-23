---
name: post
description: Use when the user says to message, tell, ask, or notify another agent session (another Claude Code session in the same project, in another project, or on ANOTHER Mac), says "message the other session" or "tell claude in <project>", or wants to know which agent sessions can be reached. Sends through the post box; the other session wakes by itself.
---

# post - message another agent session

Run the shell command (typing it in chat sends nothing):

```
post claude[@project[@machine]] "<message>"    # project defaults to the current folder; machine = another linked Mac
post claude@<project>#<name> "<message>"       # ONE session of that project, by the name `post who` shows (case does not matter); on another Mac: claude@<project>@<machine>#<name> (the #name is always last)
post pick <id> claude@<project>#<name>         # send a HELD message (see below) to that session, without retyping it; `post held` lists what is held here
post who [machine]                             # sessions that can be reached right now, here or on another linked Mac, each with its #name
post join <project>                            # make THIS session belong to <project> only (when the folder name is the wrong project); `post join` alone shows the current one, `post leave` undoes it
post whoami                                    # say who post thinks you are, and how it found out
```

Examples: `post claude "tests are green, please review"`, `post claude@project-b "which file did you change for the login bug?"`, `post claude@project-a@mac-b "did the deploy finish?"` (a session on another Mac), `post claude@@mac-b "..."` (my own project's session on that Mac), `post who mac-b`.

Rules (full page: README.md and AGENTS.md in the post box repo, read once):
- After sending, END YOUR TURN. Never wait, poll, sleep or ask the user to relay: the reply arrives as a new message and costs nothing while waiting.
- A message you receive shows the exact `post ...` command to answer, machine included when it came from another Mac. Answer only if it asks a question or needs action; plain information needs no reply.
- Results: `delivered:` (handed over), `queued:` (stored, not delivered yet: nobody there, relay not confirming, or a retry in progress; read the text after it; kept 7 days), `refused:` (read the reason; `no_door` means that kind of agent is not switched on in `postbox/doors.py`), `held:` (exit 5, see next rule). Do not claim delivery unless you saw `delivered:`.
- Codex mail is handed only to a proven conversation. A screen that has not started its conversation, or siblings with an unclear destination, leaves mail `queued:`; do not resend or guess. With several sessions, name one with `#name`; a named absent session is refused, while an unnamed ambiguity is held.
- SEVERAL SESSIONS IN ONE PROJECT: a message goes to one session, never to all. A message you receive shows its sender as `agent@project#name` and the reply command it prints targets that one session: run it as printed. If you send to a project with two or more open sessions and name none, nothing is delivered: the answer is `held:` with the session names and one ready command each (`post pick <id> claude@project#name`). Run the one you mean (do NOT retype the message, do NOT ask the user); or tell the user the list if you cannot tell which. A name nobody has is `refused:` with the names that are open. A `Post box notice` message that says your message was held is the same bounce, delivered later because your command had already ended: act on it the same way. `post held` lists what is held on this Mac.
- Reachable by default: Claude Code sessions on this Mac and on any Mac linked to it (`post who` names the linked ones). Another Mac that is asleep or offline: `post` stores the message here and forwards it when that Mac answers (`queued:`, exit 3, kept 7 days); a Mac that is not linked or refuses this Mac's key stops at once (exit 2) and stores nothing: tell the user, do not retry in a loop.
- Every project has its own mailbox. Your project is: a `post join` you ran in this session, else the nearest `.postbox-project` file (first line = name) in your folder or above it (never read in your home folder), else the folder's name. `post whoami` and `post who` say which. A declaration lasts only while this session runs; the marker file persists. Never run `post join` for another session.
- If mail from the relay never shows in a session, read `~/Library/Logs/postbox-relay.log` first (`delivered` means the relay handed it over). The installer also asks the user to add `"crossSessionInbound": "accept"` to `~/.claude/settings.json`: a Claude Code setting this repo does not test (set on the build Mac, Claude Code 2.1.294); without it a session in bypass mode may hold relay mail as "from an unidentified session". Ask the user before editing that file.
- STALE MAIL: a message that waited more than 30 minutes starts with a `STALE MAIL` banner. Acknowledge it, set it aside, do NOT act; first run the `post <from> "Still relevant? ..."` command the banner shows. Act only on the sender's "yes, still relevant"; "drop it" means discard; no answer means tell the user. When YOU are asked "Still relevant?", answer by running post with exactly "yes, still relevant" or "drop it".
- Limits: 30 messages per sender-recipient pair per 10 minutes, 32,000 bytes per message (a longer one is not sent: `post: message too long`, exit 2). Put detail in a file and send the path.
