from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "ha_observer" / "rootfs" / "app"
sys.path.insert(0, str(APP_ROOT))

from observer.store import EventStore  # noqa: E402


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_persists_only_redacted_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EventStore(str(Path(temp_dir) / "events.db"), retention_days=30)
            await store.initialize()
            now = time.time()
            await store.upsert(
                {
                    "timestamp": now,
                    "first_occurred": now,
                    "level": "ERROR",
                    "name": "homeassistant.components.home_connect",
                    "message": ["Failed via 192.168.1.20 token=verysecretvalue"],
                    "source": ["components/home_connect/api.py", 42],
                    "exception": "Bearer abcdefghijklmnopqrstuvwxyz",
                    "count": 1,
                }
            )
            entries = await store.query(search="home_connect")
            self.assertEqual(len(entries), 1)
            serialized = str(entries[0])
            self.assertNotIn("192.168.1.20", serialized)
            self.assertNotIn("verysecretvalue", serialized)
            self.assertNotIn("abcdefghijklmnopqrstuvwxyz", serialized)

    async def test_upsert_updates_duplicate_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = EventStore(str(Path(temp_dir) / "events.db"))
            await store.initialize()
            now = time.time()
            base = {
                "timestamp": now,
                "first_occurred": now,
                "level": "WARNING",
                "name": "test.integration",
                "message": ["retry"],
                "source": ["test.py", 1],
                "exception": "",
                "count": 1,
            }
            await store.upsert(base)
            await store.upsert({**base, "timestamp": now + 10, "count": 3})
            entries = await store.query()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["count"], 3)


if __name__ == "__main__":
    unittest.main()
