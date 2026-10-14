"""Which delivery doors the post box has switched on.

A "door" is the way the relay hands a message to one kind of agent (Claude Code, OpenAI Codex, Grok CLI, Grok Bot desktop).
Only the doors listed in ENABLED_DOORS are active. The code for every door stays in this repo and stays tested; the other
doors are simply switched off.

To switch a door on:
  1. Remove the leading "# " from its line below.
  2. Run `python3 install.py` again (it installs that platform's skill and re-reads the relay's PATH).

POSTBOX_DOORS (a comma-separated list, for example `POSTBOX_DOORS=claude,codex`) replaces this list, but only for the process
that has it in its environment: a test, or a relay you start by hand in a terminal. It cannot switch a door on for the installed
relay (the launchd service), and the relay is what accepts or refuses mail, so `POSTBOX_DOORS=claude,codex post codex "hello"`
still ends in `no_door`. For the installed post box, edit the list below and run `python3 install.py` again.

Mail addressed to an agent whose door is off is refused with the reason `no_door`.
"""
import os

ENABLED_DOORS = {
    "claude",
    # "codex",            # OpenAI Codex CLI (needs `codex` on PATH)
    # "grok",             # xAI Grok CLI (inbox file, no wake)
    # "grokbot-desktop",  # Grok Bot desktop via webhook (needs wake.json)
}

ALL_DOORS = ("claude", "codex", "grok", "grokbot-desktop")   # every door the code knows about, on or off
ENV_NAME = "POSTBOX_DOORS"


def enabled_names():
    """The set of enabled door names: POSTBOX_DOORS when it is set (even to an empty list), else ENABLED_DOORS."""
    raw = os.environ.get(ENV_NAME)
    if raw is not None:
        return {part.strip().lower() for part in raw.split(",") if part.strip()}
    return {str(name).strip().lower() for name in ENABLED_DOORS}


def enabled(name):
    """True when the door for this agent name is switched on."""
    return str(name).strip().lower() in enabled_names()


def describe():
    """One line for help and install output, for example 'claude (off: codex, grok, grokbot-desktop)'."""
    on = enabled_names()
    off = [name for name in ALL_DOORS if name not in on]
    return ", ".join(name for name in ALL_DOORS if name in on) + (f" (off: {', '.join(off)})" if off else "")
