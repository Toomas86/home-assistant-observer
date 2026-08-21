from __future__ import annotations

import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
RUN_SCRIPT = PROJECT / "ha_observer" / "run.sh"


class AppStartupTests(unittest.TestCase):
    def test_config_path_is_set_before_options_are_read(self) -> None:
        source = RUN_SCRIPT.read_text(encoding="utf-8")
        config_path = 'export CONFIG_PATH="/data/options.json"'
        first_config_read = "bashio::config 'tunnel_id'"

        self.assertIn(config_path, source)
        self.assertIn(first_config_read, source)
        self.assertLess(source.index(config_path), source.index(first_config_read))

    def test_supervisor_api_access_is_not_requested(self) -> None:
        config = (PROJECT / "ha_observer" / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("hassio_api: true", config)


if __name__ == "__main__":
    unittest.main()
