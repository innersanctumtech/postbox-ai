# History

## 2.1.0

- Adds the optional GitHub Copilot inbox door. A Copilot reader identifies itself with `post --as copilot@<project>`; its first inbox read registers an empty inbox so the project can receive mail before any message has arrived.
- Reports inbox-only destinations by their own agent name. Ordinary Copilot routing has one inbox destination, so it is not held for ambiguous-session selection.
- Enforces the seven-day limit using a valid original Copilot send time; missing, malformed, or future timestamps use inbox arrival time. A sender clock more than seven days behind can trigger an expired refusal. A Copilot inbox pick requires a valid send time, clamps a future time to arrival, and returns `pick_expired` after the original limit.
- Keeps the default door set unchanged: only Claude is enabled until an operator enables another door in `postbox/doors.py`.

This release provides inbox storage and reading instructions. It does not wake a Copilot session or confirm that Copilot read a message.

## 2.0.0

Postbox 2.0.0 carries portable v0.3.2 functionality in the public project structure.

- Matches messages to the exact Codex conversation across multiple screens, using process and thread evidence with safe reuse protection.
- Moves relay maintenance work off the message path and prevents sustained batches from starving other work.
- Reports explicit send outcomes and requires exact delivery evidence before reporting delivery; uncertain handoffs remain visible and have bounded recovery.
- Adds delivery fences and confirmation records for handoffs that require verified follow-through.
- Adds the Grok CLI door and metadata-only diagnostics for messages that remain stuck.

The release notes describe shipped source capabilities. They do not establish the readiness or live behavior of any particular installation.

An unseen-delivery flag clears when a valid context claim for that exact conversation was written after the verdict, or when that conversation's own log was written more than 30 seconds after the verdict. User or assistant text containing a delivery reference does not clear the flag.

Validation note: on this Mac, `tests/test_env_guard.py` reports 127 passed and 1 failed. The failing assertion expects a kernel-level socket denial that this environment does not provide. The same result was observed on the public main baseline, so this is a pre-existing environment-dependent test limitation, not a regression in this release candidate. Do not report the suite as fully green.
