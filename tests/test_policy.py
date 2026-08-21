from __future__ import annotations

import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "ha_observer" / "rootfs" / "app"
sys.path.insert(0, str(APP_ROOT))

from observer.policy import (  # noqa: E402
    filter_entity_ids,
    is_sensitive_entity,
    validate_entity_id,
    validate_trace_item_id,
)


class PolicyTests(unittest.TestCase):
    def test_validates_entity_id(self) -> None:
        self.assertEqual(validate_entity_id("Sensor.Washer_State"), "sensor.washer_state")
        with self.assertRaises(ValueError):
            validate_entity_id("sensor.bad/path")

    def test_blocks_sensitive_domains_by_default(self) -> None:
        allowed, blocked = filter_entity_ids(
            ["sensor.washer", "person.toomas", "camera.front"],
            allow_sensitive=False,
        )
        self.assertEqual(allowed, ["sensor.washer"])
        self.assertEqual(blocked, ["person.toomas", "camera.front"])
        self.assertTrue(is_sensitive_entity("device_tracker.phone"))

    def test_normalizes_automation_id(self) -> None:
        self.assertEqual(validate_trace_item_id("automation.pesumasin_odavstart"), "pesumasin_odavstart")
        with self.assertRaises(ValueError):
            validate_trace_item_id("../../secrets")


if __name__ == "__main__":
    unittest.main()
