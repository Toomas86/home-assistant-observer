from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "ha_observer" / "rootfs" / "app"
sys.path.insert(0, str(APP_ROOT))

from observer.redaction import REDACTED, redact, redact_text  # noqa: E402


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_keys_recursively(self) -> None:
        result = redact(
            {
                "access_token": "top-secret",
                "nested": {"password": "hunter2", "safe": "kept"},
                "latitude": 59.437,
            }
        )
        self.assertEqual(result["access_token"], REDACTED)
        self.assertEqual(result["nested"]["password"], REDACTED)
        self.assertEqual(result["nested"]["safe"], "kept")
        self.assertEqual(result["latitude"], REDACTED)

    def test_redacts_tokens_and_network_identifiers_in_text(self) -> None:
        raw = (
            "Authorization: Bearer abcdefghijklmnop; token=supersecretvalue "
            "at 192.168.1.25 mac AA:BB:CC:DD:EE:FF and fd00::1234 "
            "url=https://example.test/?api_key=abcdef123456"
        )
        result = redact_text(raw)
        self.assertNotIn("abcdefghijklmnop", result)
        self.assertNotIn("supersecretvalue", result)
        self.assertNotIn("192.168.1.25", result)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", result)
        self.assertNotIn("fd00::1234", result)
        self.assertNotIn("abcdef123456", result)

    def test_does_not_corrupt_timestamps(self) -> None:
        value = "2026-08-21T12:34:56+00:00"
        self.assertEqual(redact_text(value), value)

    def test_bounds_large_values(self) -> None:
        self.assertIn("truncated", redact_text("x" * 25_000).lower())
        result = redact(list(range(600)), max_items=10)
        self.assertEqual(len(result), 11)
        self.assertIn("TRUNCATED", result[-1])


if __name__ == "__main__":
    unittest.main()
