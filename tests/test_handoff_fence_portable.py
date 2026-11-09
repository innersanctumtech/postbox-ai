"""Synthetic checks for the portable fence helpers."""
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from postbox.delivery_status import lookup as delivery_lookup
from postbox.handoff_fence import HandoffFence
from postbox.handshake import ConfirmationLedger, Refused
from postbox.prepared_watch import WatchPreparationError, prepare_watch
from postbox.verified_thread_status import read_verified_status


class FencePortabilityTests(unittest.TestCase):
    def test_reservation_is_uncertain_until_token_acceptance(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            fence = HandoffFence.initialize(base / "fence.db")
            args = ("stream-a", 1, "subject-a", "message-a")
            state, token = fence.reserve(*args)
            self.assertEqual(state, "new")
            self.assertEqual(fence.reserve(*args), ("uncertain", None))
            fence.accept(*args, token)
            self.assertEqual(fence.lookup(*args), "accepted")
            self.assertEqual(fence.reserve(*args), ("accepted", None))

    def test_confirmation_ledger_reserves_one_active_recipient(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            base.chmod(0o700)
            now = [100.0]
            ledger = ConfirmationLedger.initialize(
                base / "confirmation.db", clock=lambda: now[0],
                canonical_identity=lambda identity: True,
            )
            identity = ("agent@project@machine#session", "thread-a", "a" * 64)
            request = ledger.create(identity, "short handoff summary", b"synthetic body", ttl=60,
                                    message_id="message-a", original_sent_at=100.0)
            self.assertEqual(ledger.status(request.id), "pending")
            with self.assertRaises(Refused):
                ledger.create(identity, "another short summary", b"another synthetic body", ttl=60,
                              message_id="message-b", original_sent_at=100.0)

    def test_watch_binds_marker_and_payload_digest_to_boundary(self):
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "events.log"
            log.write_bytes(b"")
            watch, payload = prepare_watch(
                target="agent@project@machine#session", thread="thread-a",
                incarnation="a" * 64, recipient_pid=1234, rollout=str(log), offset=0,
                body="synthetic payload", sender_addr="sender@project@machine",
                started_at=10.0, marker="postbox-" + "b" * 48,
            )
            self.assertIn(watch["marker"], payload)
            self.assertEqual(len(watch["payload_digest"]), 64)
            log.write_bytes(b"changed")
            with self.assertRaises(WatchPreparationError):
                prepare_watch(
                    target="agent@project@machine#session", thread="thread-a",
                    incarnation="a" * 64, recipient_pid=1234, rollout=str(log), offset=0,
                    body="synthetic payload", sender_addr="sender@project@machine",
                    started_at=11.0,
                )

    def test_delivery_lookup_requires_unique_correlation_and_all_seen_ids(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "events.jsonl"
            events = [
                {"event": "delivery_correlation", "message_id": "message-a", "delivery_ids": ["d1", "d2"]},
                {"event": "recipient_seen", "id": "d1"},
            ]
            path.write_text("\n".join(json.dumps(row) for row in events) + "\n")
            self.assertEqual(delivery_lookup(path, "message-a"), {"state": "handoff", "delivery_ids": ["d1", "d2"]})
            events.append({"event": "recipient_seen", "id": "d2"})
            path.write_text("\n".join(json.dumps(row) for row in events) + "\n")
            self.assertEqual(delivery_lookup(path, "message-a")["state"], "appeared")

    def test_verified_status_revalidates_and_closes_connection(self):
        class Lease:
            def __init__(self):
                self.calls = 0

            def revalidate(self):
                self.calls += 1
                return True

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        class Provider:
            def __init__(self):
                self.lease = Lease()
                self.connection = Connection()

            def connect_verified(self, *, timeout):
                return self.connection, self.lease

            def read_thread_status(self, connection, thread_id, *, timeout):
                return {"thread": thread_id, "status": "ready"}

        provider = Provider()
        self.assertEqual(read_verified_status(provider, "thread-a"), {"thread": "thread-a", "status": "ready"})
        self.assertEqual(provider.lease.calls, 2)
        self.assertTrue(provider.connection.closed)


if __name__ == "__main__":
    unittest.main()
