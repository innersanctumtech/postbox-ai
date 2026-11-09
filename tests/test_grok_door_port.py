"""Synthetic checks for the standalone Grok inbox door adapter."""
from dataclasses import dataclass
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from postbox.grok_door import GrokDoor
from postbox import doors


@dataclass
class Session:
    project: str
    pid: int
    label: str


class GrokDoorPortTests(unittest.TestCase):
    def make_door(self, **overrides):
        options = {
            "sessions_provider": lambda: [],
            "same_project": lambda left, right: left == right,
            "inbox_writer": lambda *args: None,
            "renderer": lambda *args, **kwargs: "rendered",
        }
        options.update(overrides)
        return GrokDoor(**options)

    def test_resolve_selects_one_matching_project_and_excludes_sender(self):
        sessions = [
            Session("project-a", 11, "newest"),
            Session("project-a", 12, "sender"),
            Session("project-b", 13, "other project"),
        ]
        door = self.make_door(sessions_provider=lambda: sessions)

        self.assertEqual(door.resolve("project-a", exclude_pid=12), [sessions[0]])

    def test_deliver_writes_to_the_project_inbox_without_extra_targets(self):
        writes = []
        door = self.make_door(inbox_writer=lambda agent, project, text: writes.append((agent, project, text)))

        door.deliver(Session("project-a", 11, "target"), "synthetic message")

        self.assertEqual(writes, [("grok", "project-a", "synthetic message")])

    def test_render_and_busy_note_explain_inbox_delivery_is_not_a_wake(self):
        renders = []
        door = self.make_door(renderer=lambda *args, **kwargs: renders.append((args, kwargs)) or "rendered")
        session = Session("project-a", 11, "grok@project-a")

        self.assertIn("cannot be woken", door.busy_note(session))
        self.assertEqual(door.render("claude@project-a", "hello", waited_s=4), "rendered")
        self.assertEqual(renders, [(("claude@project-a", "hello"), {"inbox": True, "waited_s": 4})])

    def test_grok_stays_off_in_the_public_default_configuration(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("grok", doors.enabled_names())
            self.assertFalse(doors.enabled("grok"))


if __name__ == "__main__":
    unittest.main()
