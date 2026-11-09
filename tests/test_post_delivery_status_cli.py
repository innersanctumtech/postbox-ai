"""Synthetic checks for the sender-side delivery status command."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from postbox import post  # noqa: E402


class DeliveryStatusCliTests(unittest.TestCase):
    def test_status_reads_synthetic_correlation_and_seen_records(self):
        with tempfile.TemporaryDirectory() as directory:
            event_path = Path(directory) / "events.jsonl"
            records = [
                {"event": "delivery_correlation", "message_id": "message-a", "delivery_ids": ["receipt-a", "receipt-b"]},
                {"event": "recipient_seen", "id": "receipt-a"},
            ]
            event_path.write_text("".join(json.dumps(row) + "\n" for row in records))
            with patch.object(post.lib, "relay_log_path", return_value=str(event_path)), \
                    patch.object(post.lib, "resolve_sender", side_effect=AssertionError("sender lookup should not run")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    post.main(["post", "status", "message-a"])
                self.assertIn("HANDOFF:", output.getvalue())

                records.append({"event": "recipient_seen", "id": "receipt-b"})
                event_path.write_text("".join(json.dumps(row) + "\n" for row in records))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    post.main(["post", "status", "message-a"])
                self.assertIn("APPEARED:", output.getvalue())
                self.assertIn("acknowledgement", output.getvalue())

                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    post.main(["post", "status", "missing"])
                self.assertEqual(raised.exception.code, 6)
                self.assertIn("UNKNOWN:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
