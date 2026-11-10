#!/usr/bin/env python3
"""Synthetic checks for the Copilot inbox door and its relay integration."""
import asyncio
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "postbox"))
os.environ["POSTBOX_TELEMETRY"] = "0"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
import _env  # noqa: E402,F401
import doors  # noqa: E402
import lib  # noqa: E402
import post as post_cli  # noqa: E402
import relay  # noqa: E402


class FakeMessage:
    def __init__(self, subject, headers, body="synthetic note"):
        self.subject = subject
        self.headers = headers
        self.data = body.encode()
        self.metadata = types.SimpleNamespace(num_delivered=1, sequence=types.SimpleNamespace(stream=1, consumer=1))
        self.acked = False
        self.nacked = False
        self.termed = False

    async def ack(self):
        self.acked = True

    async def nak(self, delay=None):
        self.nacked = delay

    async def term(self):
        self.termed = True


class FakeNC:
    def __init__(self):
        self.replies = []

    async def publish(self, subject, data):
        self.replies.append((subject, json.loads(data)))

    async def flush(self, timeout=None):
        pass


class CopilotDoorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        os.environ["POSTBOX_INBOX_DIR"] = str(root / "inbox")
        os.environ["POSTBOX_SUPPORT_DIR"] = str(root / "support")
        os.environ["POSTBOX_STUCK_NOTICES"] = str(root / "stuck.json")
        self.copilot = lib.DOORS["copilot"]

    def tearDown(self):
        self.temp.cleanup()

    def test_door_registration_and_destinations(self):
        self.assertTrue(issubclass(lib.CopilotDoor, lib.InboxOnlyDoor))
        self.assertTrue(issubclass(lib.GrokbotDesktopDoor, lib.InboxOnlyDoor))
        self.assertIsInstance(self.copilot, lib.CopilotDoor)
        self.assertTrue(self.copilot.inbox_only)
        self.assertTrue(self.copilot.register_on_read)
        self.assertIn("copilot", doors.ALL_DOORS)
        self.assertTrue(lib.inbox_register("copilot", "project-a"))
        self.assertFalse(lib.inbox_register("claude", "project-b"))
        self.assertTrue(lib.inbox_dir("copilot", "project-a").is_dir())
        self.assertIn(("copilot", "project-a"), lib.inbox_only_destinations())
        self.assertIn("project-a", lib.inbox_only_projects())
        self.assertIn(("copilot", "project-a", 0), lib.inbox_only_agents())

    def test_new_destination_list_canonicalizes_aliases_and_skips_symlinks(self):
        lib.inbox_dir("copilot", "project-old").mkdir(parents=True)
        alias_file = lib.support_dir() / "aliases.json"
        alias_file.parent.mkdir(parents=True, exist_ok=True)
        alias_file.write_text(json.dumps({"project-old": "project-new"}))
        (lib.inbox_root() / "copilot.project-link").symlink_to(
            lib.inbox_root() / "copilot.project-old", target_is_directory=True)
        destinations = lib.inbox_only_destinations()
        self.assertIn(("copilot", "project-new"), destinations)
        self.assertNotIn(("copilot", "project-old"), destinations)
        self.assertNotIn(("copilot", "project-link"), destinations)

    def test_existing_who_list_keeps_raw_alias_folder_and_waiting_count(self):
        lib.inbox_put("grokbot-desktop", "project-old", "synthetic waiting message")
        alias_file = lib.support_dir() / "aliases.json"
        alias_file.parent.mkdir(parents=True, exist_ok=True)
        alias_file.write_text(json.dumps({"project-old": "project-new"}))
        self.assertIn(("grokbot-desktop", "project-old", 1), lib.inbox_only_agents())

    def test_registered_inbox_directories_are_mode_0700(self):
        self.assertTrue(lib.inbox_register("copilot", "project-mode"))
        self.assertEqual(stat.S_IMODE(lib.inbox_root().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(lib.inbox_dir("copilot", "project-mode").stat().st_mode), 0o700)

    def test_bad_inbox_usage_does_not_register_a_folder(self):
        folder = lib.inbox_dir("copilot", "project-bad-use")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            post_cli.cmd_inbox(["--unknown"], ("copilot", "project-bad-use", None))
        self.assertFalse(folder.exists())

    def test_future_sent_at_is_not_accepted_as_original_send_time(self):
        now = 2_000_000_000
        with mock.patch.object(relay.time, "time", return_value=now):
            self.assertIsNone(relay.original_sent_timestamp({"Sent-At": str(now + 60)}))
            self.assertIsNone(relay.original_sent_timestamp({"Sent-At": str(now + 10_800)}))
        stored = lib.inbox_put("copilot", "project-arrival", "synthetic", now=now,
                               original_sent_at=now + 60)
        self.assertEqual(int(stored.name[:13]) / 1000, now)
        with self.assertRaises(ValueError):
            lib.inbox_put("copilot", "project-invalid-time", "synthetic", now=now,
                          original_sent_at=float("nan"))
        self.assertFalse(lib.inbox_dir("copilot", "project-invalid-time").exists())

    def test_read_command_and_rendering_name_the_copilot_inbox(self):
        self.assertEqual(self.copilot.read_cmd("project-a"), "post --as copilot@project-a inbox")
        self.assertIn("post --as copilot@project-a", self.copilot.busy_note(self.copilot.resolve("project-a")[0]))
        body = self.copilot.render("claude@project-b", "synthetic note", project="project-a")
        self.assertIn("post --as copilot@project-a claude@project-b", body)
        self.assertIn("post --as copilot@project-a inbox", body)
        self.assertIn("copilot@project-a", self.copilot.resolve("project-a")[0].label)

    def test_copilot_delivery_does_not_attempt_a_wake_webhook(self):
        class Waker:
            calls = 0

            def configured(self, agent, project):
                self.calls += 1
                return True

        waker = Waker()
        with mock.patch.object(relay, "WAKER", waker):
            result = asyncio.run(relay.ring_wake(self.copilot, "copilot", "project-a", "claude@project-b"))
        self.assertEqual(result, "")
        self.assertEqual(waker.calls, 0)

    def test_relay_delivery_stores_mail_in_the_copilot_inbox(self):
        sent_at = int(__import__("time").time()) - 120
        msg = FakeMessage("post.project-a.copilot", {
            "From": "claude@project-b", "From-Pid": "123", "Reply-Inbox": "_INBOX.test",
            "Nats-Msg-Id": "synthetic-copilot-delivery", "Sent-At": str(sent_at),
        })
        nc = FakeNC()
        asyncio.run(relay.handle(nc, msg, lib.DOORS, lib.PairLimiter()))
        self.assertTrue(msg.acked)
        self.assertEqual(nc.replies[0][1]["status"], "delivered")
        self.assertIn("cannot be woken", nc.replies[0][1]["detail"])
        stored = [path.read_text() for path in lib.inbox_list("copilot", "project-a")]
        self.assertEqual(len(stored), 1)
        self.assertIn("synthetic note", stored[0])
        self.assertIn("post --as copilot@project-a inbox", stored[0])
        paths = lib.inbox_list("copilot", "project-a")
        self.assertEqual(int(paths[0].name[:13]) / 1000, sent_at)

    def test_first_explicit_read_registers_an_empty_inbox(self):
        with mock.patch.object(lib, "my_identity", return_value=None), contextlib.redirect_stdout(io.StringIO()) as output:
            post_cli.main(["post", "--as", "copilot@project-b", "inbox"])
        self.assertIn("No messages waiting for copilot@project-b", output.getvalue())
        self.assertTrue(lib.inbox_dir("copilot", "project-b").is_dir())
        self.assertIn(("copilot", "project-b"), lib.inbox_only_destinations())

    def test_history_and_watch_reads_register_before_access(self):
        with contextlib.redirect_stdout(io.StringIO()):
            post_cli.cmd_inbox(["--history"], ("copilot", "project-c", None))
        self.assertTrue(lib.inbox_dir("copilot", "project-c").is_dir())
        with mock.patch.object(post_cli, "watch_inbox"), mock.patch.object(post_cli, "emit_command"), \
             contextlib.redirect_stdout(io.StringIO()):
            post_cli.cmd_inbox(["--watch"], ("copilot", "project-d", None))
        self.assertTrue(lib.inbox_dir("copilot", "project-d").is_dir())

    def test_relay_who_reports_each_inbox_agents_own_name(self):
        lib.inbox_register("copilot", "project-a")
        lib.inbox_dir("grokbot-desktop", "project-b").mkdir(parents=True)
        nc = FakeNC()
        request = types.SimpleNamespace(reply="_INBOX.who")
        asyncio.run(relay.answer_who(nc, request))
        inboxes = {(row["agent"], row["project"]) for row in nc.replies[0][1]["inbox_only"]}
        self.assertIn(("copilot", "project-a"), inboxes)
        self.assertIn(("grokbot-desktop", "project-b"), inboxes)

    def test_relay_who_canonicalizes_alias_and_keeps_raw_folder_waiting_count(self):
        lib.inbox_put("grokbot-desktop", "project-old", "synthetic waiting note")
        alias_file = lib.support_dir() / "aliases.json"
        alias_file.parent.mkdir(parents=True, exist_ok=True)
        alias_file.write_text(json.dumps({"project-old": "project-new"}))
        (lib.inbox_root() / "grokbot-desktop.project-link").symlink_to(
            lib.inbox_root() / "grokbot-desktop.project-old", target_is_directory=True)
        nc = FakeNC()
        request = types.SimpleNamespace(reply="_INBOX.who")
        asyncio.run(relay.answer_who(nc, request))
        inboxes = nc.replies[0][1]["inbox_only"]
        self.assertIn({"agent": "grokbot-desktop", "project": "project-new", "waiting": 1}, inboxes)
        self.assertFalse(any(row["project"] == "project-link" for row in inboxes))

    def test_who_does_not_show_wake_status_for_copilot(self):
        async def who_response():
            return {"sessions": [], "inbox_only": [
                {"agent": "copilot", "project": "project-a", "waiting": 0},
                {"agent": "grokbot-desktop", "project": "project-b", "waiting": 0},
            ]}

        with mock.patch.object(post_cli, "local_relay_who", new=who_response), \
             mock.patch.object(post_cli.lib, "my_identity", return_value=None), \
             mock.patch.object(post_cli.wake, "load_wake", return_value={}), \
             mock.patch.object(post_cli.lib, "load_link", return_value={}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            post_cli.cmd_who([])
        lines = output.getvalue().splitlines()
        copilot = next(line for line in lines if "copilot@project-a" in line)
        grokbot = next(line for line in lines if "grokbot-desktop@project-b" in line)
        self.assertIn("cannot be woken", copilot)
        self.assertNotIn("wake:", copilot)
        self.assertIn("wake: not configured", grokbot)

    def test_held_inbox_delivery_preserves_the_original_expiry(self):
        now = 2_000_000_000
        deadline = now + 60
        target = self.copilot.resolve("project-a")[0]
        with mock.patch.object(relay.time, "time", return_value=now):
            sent = relay.deliver_inbox_before_deadline(self.copilot, target, "held synthetic note", deadline,
                                                        original_sent_at=now - 10)
        self.assertTrue(sent)
        paths = lib.inbox_list("copilot", "project-a", now=now)
        self.assertEqual(len(paths), 1)
        self.assertEqual(int(paths[0].name[:13]) / 1000, now - 10)

    def test_held_inbox_delivery_stops_at_expiry(self):
        target = self.copilot.resolve("project-a")[0]
        with mock.patch.object(relay.time, "time", return_value=100):
            sent = relay.deliver_inbox_before_deadline(self.copilot, target, "expired synthetic note", 100)
        self.assertFalse(sent)
        self.assertEqual(lib.inbox_list("copilot", "project-a"), [])

    def test_expired_held_inbox_delivery_cancels_its_fence(self):
        now = 100
        target = self.copilot.resolve("project-a")[0]

        class Fence:
            def __init__(self):
                self.reserved = False
                self.cancelled = False
                self.accepted = False

            def reserve(self, *args):
                self.reserved = True
                return "new", "synthetic-token"

            def cancel_before_send(self, *args):
                self.cancelled = True

            def accept(self, *args):
                self.accepted = True

        fence = Fence()
        msg = FakeMessage("post.project-a.copilot", {"Nats-Msg-Id": "synthetic-expired-fence"})
        with mock.patch.object(relay, "_handoff_fence", return_value=fence), \
             mock.patch.object(relay.time, "time", return_value=now), \
             self.assertRaises(relay.HeldDeliveryExpired):
            asyncio.run(relay.fenced_deliver(msg, self.copilot, target, "synthetic expired note",
                                              "synthetic-expired-fence", deadline=now,
                                              original_sent_at=now - 1))
        self.assertTrue(fence.reserved)
        self.assertTrue(fence.cancelled)
        self.assertFalse(fence.accepted)
        self.assertEqual(lib.inbox_list("copilot", "project-a", now=now), [])

    def test_upfront_expired_pick_refuses_before_reserving_the_fence(self):
        sent_at = int(time.time()) - lib.MAX_AGE_S - 10
        held = {"id": "expired1", "frm": "claude@project-b", "agent": "copilot", "project": "project-a",
                "session": "", "text": "held synthetic note", "sent_at": sent_at}
        msg = FakeMessage("post.project-a.copilot", {
            "From": "claude@project-b", "Reply-Inbox": "_INBOX.expired", "Nats-Msg-Id": "synthetic-upfront-expiry",
            "Sent-At": str(sent_at), "Pick": "expired1", "To-Session": "reader",
        })
        nc = FakeNC()

        class Fence:
            def __init__(self):
                self.reserved = False

            def reserve(self, *args):
                self.reserved = True
                return "new", "synthetic-token"

            def cancel_before_send(self, *args):
                pass

        fence = Fence()
        with mock.patch.object(lib, "hold_get", return_value=held), \
             mock.patch.object(relay, "_handoff_fence", return_value=fence):
            asyncio.run(relay.handle(nc, msg, lib.DOORS, lib.PairLimiter()))
        self.assertTrue(msg.termed)
        self.assertFalse(msg.acked)
        self.assertFalse(fence.reserved)
        self.assertEqual(nc.replies[0][1]["status"], "refused")
        self.assertEqual(nc.replies[0][1]["reason"], "pick_expired")

    def test_copilot_pick_rejects_missing_or_invalid_original_time(self):
        now = int(time.time())

        class Fence:
            def reserve(self, *args):
                raise AssertionError("invalid held timestamp must be refused before fencing")

        for value in (None, "not-a-time", float("nan")):
            with self.subTest(sent_at=value):
                held = {"id": "invalid1", "frm": "claude@project-b", "agent": "copilot", "project": "project-a",
                        "session": "", "text": "held synthetic note"}
                if value is not None:
                    held["sent_at"] = value
                msg = FakeMessage("post.project-a.copilot", {
                    "From": "claude@project-b", "Reply-Inbox": "_INBOX.invalid", "Nats-Msg-Id": "synthetic-invalid-time",
                    "Sent-At": str(now), "Pick": "invalid1", "To-Session": "reader",
                })
                nc = FakeNC()
                with mock.patch.object(lib, "hold_get", return_value=held), \
                     mock.patch.object(relay, "_handoff_fence", return_value=Fence()):
                    asyncio.run(relay.handle(nc, msg, lib.DOORS, lib.PairLimiter()))
                self.assertTrue(msg.termed)
                self.assertEqual(nc.replies[0][1]["reason"], "pick_expired")
                self.assertEqual(lib.inbox_list("copilot", "project-a"), [])

    def test_copilot_pick_clamps_future_time_and_grokbot_pick_keeps_legacy_path(self):
        now = int(time.time())

        class Fence:
            def reserve(self, *args):
                return "new", "synthetic-token"

            def accept(self, *args):
                pass

            def cancel_before_send(self, *args):
                raise AssertionError("future timestamp must be clamped, not expired")

        for agent, sent_at in (("copilot", now + 3600),
                               ("grokbot-desktop", now - lib.MAX_AGE_S - 10)):
            with self.subTest(agent=agent):
                held = {"id": "picked1", "frm": "claude@project-b", "agent": agent, "project": "project-a",
                        "session": "", "text": "held synthetic note", "sent_at": sent_at}
                msg = FakeMessage(f"post.project-a.{agent}", {
                    "From": "claude@project-b", "Reply-Inbox": "_INBOX.picked", "Nats-Msg-Id": f"synthetic-{agent}-pick",
                    "Sent-At": str(now), "Pick": "picked1", "To-Session": "reader",
                })
                nc = FakeNC()
                with mock.patch.object(lib, "hold_get", return_value=held), \
                     mock.patch.object(lib, "hold_delete", return_value=True), \
                     mock.patch.object(relay, "_handoff_fence", return_value=Fence()), \
                     mock.patch.object(relay.time, "time", return_value=now):
                    asyncio.run(relay.handle(nc, msg, lib.DOORS, lib.PairLimiter()))
                self.assertTrue(msg.acked)
                self.assertEqual(nc.replies[0][1]["status"], "delivered")
                path = lib.inbox_list(agent, "project-a")
                self.assertEqual(len(path), 1)
                if agent == "copilot":
                    self.assertEqual(int(path[0].name[:13]) / 1000, now)
                else:
                    self.assertGreater(int(path[0].name[:13]) / 1000, sent_at)

    def test_picked_copilot_mail_keeps_its_original_retention_time(self):
        sent_at = int(__import__("time").time()) - 5
        held = {"id": "abc123", "frm": "claude@project-b", "agent": "copilot", "project": "project-a",
                "session": "", "text": "held synthetic note", "sent_at": sent_at}

        class Fence:
            def __init__(self):
                self.accepted = False

            def reserve(self, stream, sequence, subject, message_id):
                return "new", "synthetic-token"

            def accept(self, stream, sequence, subject, message_id, token):
                self.accepted = True

            def cancel_before_send(self, *args):
                raise AssertionError("valid held delivery was cancelled")

        fence = Fence()
        msg = FakeMessage("post.project-a.copilot", {
            "From": "claude@project-b", "From-Pid": "123", "Reply-Inbox": "_INBOX.pick",
            "Nats-Msg-Id": "synthetic-picked-delivery", "Sent-At": str(sent_at),
            "Pick": "abc123", "To-Session": "reader",
        })
        nc = FakeNC()
        with mock.patch.object(lib, "hold_get", return_value=held), \
             mock.patch.object(lib, "hold_delete", return_value=True), \
             mock.patch.object(relay, "_handoff_fence", return_value=fence):
            asyncio.run(relay.handle(nc, msg, lib.DOORS, lib.PairLimiter()))
        self.assertTrue(msg.acked)
        self.assertTrue(fence.accepted)
        paths = lib.inbox_list("copilot", "project-a", now=sent_at)
        self.assertEqual(len(paths), 1)
        self.assertEqual(int(paths[0].name[:13]) / 1000, sent_at)
        self.assertIn("held synthetic note", paths[0].read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
