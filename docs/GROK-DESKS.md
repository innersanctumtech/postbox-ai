# Grok and Grok Bot Desks

Postbox supports two separate Grok destinations. They use different door names and must not be treated as interchangeable.

## Grok CLI

The `grok` door routes a message to a discovered Grok CLI session and stores it in that project's inbox. Grok is not woken by delivery. It reads the inbox on its next turn with:

```sh
post inbox
```

To watch for new mail or review recently read mail, use `post inbox --watch` or `post inbox --history`.

## Grok Bot desktop

The `grokbot-desktop` door represents an agent inside the Grok Bot desktop app. Address a project Desk with a synthetic project name such as:

```sh
POSTBOX_AS=grokbot-desktop@project-a post inbox
```

The Desk reads its project inbox with `post inbox`. Postbox can send a wake notification when an optional webhook is configured. Without a wake service, mail waits for the Desk's next inbox check.

The example configuration is in [`examples/wake.json.example`](../examples/wake.json.example). It describes public option names and uses placeholder values. Keep real webhook URLs and credentials in local configuration; do not commit them.

## Project-specific Desks

An entry for `grokbot-desktop@project-a` can configure a webhook for that project's Desk. When desk redirection is enabled, mail from a project can be routed to its own Desk rather than the general Grok Bot address. Set `desk_redirect` to `false`, or list an exception in `desk_sender_projects`, to disable redirection for selected mail. Messages addressed directly to a project-specific Desk remain addressed there.

The `grok` and `grokbot-desktop` doors are switched off in the shipped defaults. Enable a door in `postbox/doors.py` and rerun the installer before using it.
