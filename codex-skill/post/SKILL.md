---
name: post
description: Use when the user asks you to message, tell, or ask another agent session (Claude or Codex, same project, another project, or ANOTHER Mac), says "msg <project>", or wants to know which sessions can be reached. Sends through the post box; the receiving Claude Code or Codex session gets the message in its conversation (Codex between tasks).
---

# post - message another agent session

Run this shell command (typing it in chat sends nothing):

```
post <agent>[@project[@machine]] "<message>"   # agent: claude or codex; project defaults to the current folder; machine = another Mac
post <agent>@<project>#<name> "<message>"      # ONE session of that project, by the name `post who` shows; on another Mac: <agent>@<project>@<machine>#<name> (the #name is always last)
post pick <id> <agent>@<project>#<name>        # send a HELD message to that session without retyping it; `post held` lists what is held here
post who [machine]                             # sessions that can be reached right now, here or on another linked Mac, each with its #name
post inbox                                     # a Grok session reads and clears its own mail (Claude and Codex are woken instead)
post join <project>                            # make THIS session belong to <project> only (when the folder name is the wrong project); `post join` alone shows the current one, `post leave` undoes it
post --as agent@project ...                    # name yourself when post cannot tell who is sending (Grok); `post whoami` shows what it detects
```

Examples: `post claude "build is green"`, `post codex@project-b "which file did you change?"`, `post claude@project-a@mac-b "did the deploy finish?"` (a session on another Mac), `post claude@@mac-b "..."` (my own project's session on that Mac), `post who mac-b`. Rules, read once: README.md and AGENTS.md in the post box repo.

- Every project has its own mailbox. Your project is: a `post join` you ran in this session, else the nearest `.postbox-project` file (first line = name) in your folder or above it (never read in your home folder), else the folder's name. `post whoami` and `post who` say which. A declaration lasts only while this session runs; the marker file persists. Never run `post join` for another session.
- After sending, end your turn. Never wait, poll, or ask the user to relay: the reply arrives as a new message.
- A received message shows the exact `post ...` command to answer, machine included. Run it only if the message asks a question or needs action.
- Say `delivered` only if `post` printed `delivered:`. `queued:` (stored, not delivered yet: nobody there, the other Mac asleep, the relay not confirming, or a retry in progress; read the text after it; kept 7 days) and `refused:` are not delivery.
- A Codex delivery is addressed to one proven conversation. If a screen has not started its own conversation, or sibling screens make the destination unclear, the result stays `queued:`; do not resend or guess. Name one session with `#name` when several sessions are open. A later recovery may retry once only after the relay revalidates an exact live conversation.
- Which kinds of agent can receive depends on the doors switched on in `postbox/doors.py` of the post box repo: the shipped default is `claude` only, and this skill is installed only when the `codex` door is on. Mail to an agent whose door is off is `refused:` with `no door` (reason `no_door`): do not retry, tell the user which door is off. `grok` and `grokbot-desktop` are off until the user enables them there and runs `python3 install.py` again.
- With the door on, claude and codex sessions on this Mac and on any Mac linked to it are reachable (`post who` names the linked ones). A Codex session reads mail only between tasks, so a send can end with `Heads up: ... is mid-task`; the mail waits and is read when that task ends. Grok (if its door is on) cannot be woken: mail to Grok waits in its inbox (kept 7 days) and Grok reads it when its next turn starts, so `delivered` to Grok means "waiting in its inbox".
- Another Mac asleep or offline: `post` stores the message here (`queued: ... stored here and forwarded when it answers`, exit 3) and the relay forwards it when that Mac is back: do not resend. Not linked, or key rejected: `post` says so (exit 2) and stores nothing. Tell the user; do not retry in a loop.
- A message that starts with `STALE MAIL:` waited over 30 minutes: do NOT act on it. Run the `post ...` command it shows to ask the sender "Still relevant?", then act only on `yes, still relevant`; `drop it` means discard; no answer means tell the user. If you are asked "Still relevant?", answer with exactly `yes, still relevant` or `drop it`.
- **Grok Bot Desks** (the project agents of the Grok Bot desktop app, never bare `grok`; only when the `grokbot-desktop` door is on): a project that has a Desk reaches it at `grokbot-desktop@<project>` (for example `grokbot-desktop@project-a`). The Desk reads its mail with `post inbox`; it is woken only if the user set up a wake webhook in `wake.json` (README.md, "Other doors"), otherwise the mail waits. Do not send Desk mail to the general `grokbot-desktop@<name>` address when your project has its own Desk (the box redirects it, but use the right address). Read mail stays readable for 7 days with `post inbox --history`.
- Several sessions in one project: a message goes to ONE session, never to all. A message you receive shows its sender as `agent@project#name`; run the reply command exactly as printed. If you send to a project with two or more open sessions and name none, nothing is delivered: the answer is `held:` (exit 5) with the names and one ready `post pick <id> <address>#<name>` command each. Run the one you mean; do not retype the message. A `Post box notice` message saying a message was held is the same bounce. A name nobody has is `refused:` with the names that are open.
