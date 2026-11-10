# History

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
