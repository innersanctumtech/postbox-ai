"""Reject clear destructive requests at public delivery boundaries.

This language filter is conservative and cannot replace a recipient's own
authorization checks.
"""
from __future__ import annotations

import itertools
import re


_ACTIONS = r"(?:delete|trash|wipe|purge|erase|destroy|terminate|drop|truncate)"
_REMOVE = re.compile(
    r"\b(?:remove|clean\s+out)\b[^.!?\n]{0,120}\b(?:local|workspace|project|"
    r"repo(?:sitory)?|folder|directory|archive|artifacts?|backups?|data|files?|"
    r"snapshots?|volumes?|instances?|amis?|cloud|git\s+history)\b",
    re.I,
)
_DIRECT = re.compile(
    rf"(?:^|[.!?\n][^\S\n]*)(?:please\s+|then\s+|go\s+ahead\s+and\s+)?{_ACTIONS}\b",
    re.I,
)
_REQUEST = re.compile(
    rf"\b(?:can\s+you|could\s+you|please|you\s+(?:must|should|need\s+to)|"
    rf"ask\s+\w+\s+to|tell\s+\w+\s+to|instruct\s+\w+\s+to)\b"
    rf"[^.!?\n]{{0,160}}\b{_ACTIONS}\b",
    re.I,
)
_COMMAND = re.compile(
    r"\b(?:rm\s+(?:-(?!-)[A-Za-z]+\s+|--(?!recursive\b)[A-Za-z][A-Za-z0-9-]*\s+)*(?:"
    r"--recursive(?=\s|$|[;|&])|-(?=[A-Za-z]*[rR])[A-Za-z]+\b)|git\s+clean\b|"
    r"git\s+reset\s+--hard\b|shutil\.rmtree\b|aws\s+\S+\s+(?:delete|terminate)-\S+)\b",
    re.I,
)
_DESCRIBED = re.compile(
    r"(?:\b(?:rules?|prompts?|patterns?|guards?|filters?|hooks?|checks?|warnings?)\s+(?:for|on|of|against)"
    r"|\b(?:blocks?|blocked|blocking|denies|denied|denying|matched|matches|matching|mentions?|mentioning|prompting\s+for))"
    r"\s+(?:(?:the|any|all|every)\s+)?[`'\"]*$",
    re.I,
)
_DO_IT = re.compile(
    r"\b(?:run|running|execute|executing|exec|apply|applying|invoke|issue|proceed|perform|trigger|launch|go\s+ahead|"
    r"please|pls|plz|kindly|anyway|override|do\s+(?:it|so|this|that)|do\s+the\s+same|likewise|repeat|by\s+hand|manually)\b",
    re.I,
)
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)|\n")
_DESCRIPTION_WINDOW = 60
_SENTENCE_WINDOW = 200
_MAX_COMMANDS_EXAMINED = 50


def _is_description(text: str, match: re.Match) -> bool:
    prior = text[max(0, match.start() - _DESCRIPTION_WINDOW):match.start()]
    if _DESCRIBED.search(prior) is None:
        return False
    before = text[max(0, match.start() - _SENTENCE_WINDOW):match.start()]
    after = text[match.end():match.end() + _SENTENCE_WINDOW]
    before_sentence = _SENTENCE_END.split(before)[-1]
    after_sentence = _SENTENCE_END.split(after, 1)[0]
    return _DO_IT.search(before_sentence) is None and _DO_IT.search(after_sentence) is None


def destructive_request(text: str) -> bool:
    """Return true when text clearly requests a destructive action."""
    if not isinstance(text, str):
        return False
    if _DIRECT.search(text) or _REQUEST.search(text) or _REMOVE.search(text):
        return True
    found = list(itertools.islice(_COMMAND.finditer(text), _MAX_COMMANDS_EXAMINED + 1))
    return len(found) > _MAX_COMMANDS_EXAMINED or any(not _is_description(text, match) for match in found)
