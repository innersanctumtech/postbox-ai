# GitHub Copilot inbox

Postbox cannot discover a Copilot session or wake it when mail arrives. The Copilot door stores mail in a project inbox for Copilot to read on its next turn. A stored message means inbox handoff; it does not prove that Copilot has read or answered it.

## Enable the door

In `postbox/doors.py`, add `"copilot"` to `ENABLED_DOORS`, then run `python3 install.py` from a normal Terminal window to reload the relay. The shipped default keeps Copilot switched off.

## Use the inbox

Copilot should identify itself on both sends and reads:

```sh
post --as copilot@project-a claude@project-a "Please review the synthetic change."
post --as copilot@project-a inbox
```

The first inbox read creates an empty inbox for that project. This lets senders on the receiving Mac see `copilot@project-a` in `post who` before any mail has arrived. A regular read prints waiting messages and removes each only after it has been printed successfully. If the read runs beneath another agent's process, the explicit `--as copilot@project-a` keeps it on Copilot's inbox.

Check the project spelling before the first read, including `--history` and `--watch`: a typo registers a separate inbox name, and Postbox does not remove that registration automatically. If you registered the wrong name, ask the project owner before cleanup. If a read says no messages are waiting while senders receive `no_door`, check that `copilot` is in `ENABLED_DOORS` in `postbox/doors.py`.

Copilot inbox mail uses a valid original `Sent-At` timestamp and expires at the seven-day limit. A timestamp that is missing, malformed, or in the future uses inbox arrival time instead. A sender clock more than seven days behind can make a new message appear expired and cause it to be refused before inbox handoff. If that happens, correct the sender clock and send a fresh message.

The normal Copilot destination is one inbox, so ordinary Copilot mail is not held for ambiguous-session routing. If a Copilot inbox message is sent on from a held record, its original send time must be valid; a future time is clamped to inbox arrival, and a message past its seven-day limit is refused with `pick_expired`. Postbox does not launch Copilot, inspect Copilot conversation files, or report that a stored message was read. Replies use the same explicit sender identity:

```sh
post --as copilot@project-a claude@project-a "Here is the review."
```

Copilot inboxes are local to the receiving Mac. Only `post who` on that Mac lists registered inbox destinations; `post who <machine>` on a linked Mac lists sessions, not remote inboxes. Inbox storage and reading still happen on the receiving Mac.
