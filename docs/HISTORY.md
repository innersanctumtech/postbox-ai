# History

## 2.0.0

Postbox 2.0.0 carries portable v0.3.1 functionality in the public project structure.

- Matches messages to the exact Codex conversation across multiple screens, using process and thread evidence with safe reuse protection.
- Moves relay maintenance work off the message path and prevents sustained batches from starving other work.
- Reports explicit send outcomes and requires exact delivery evidence before reporting delivery; uncertain handoffs remain visible and have bounded recovery.
- Adds delivery fences and confirmation records for handoffs that require verified follow-through.
- Adds the Grok CLI door and metadata-only diagnostics for messages that remain stuck.

The release notes describe shipped source capabilities. They do not establish the readiness or live behavior of any particular installation.
