from __future__ import annotations

import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
RUN_SCRIPT = PROJECT / "ha_observer" / "run.sh"


class AppStartupTests(unittest.TestCase):
    def test_options_are_read_from_mounted_file(self) -> None:
        source = RUN_SCRIPT.read_text(encoding="utf-8")
        commands = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn('options_file="/data/options.json"', source)
        self.assertIn("jq --raw-output '.tunnel_id // empty'", source)
        self.assertIn("jq --raw-output '.openai_runtime_api_key // empty'", source)
        self.assertNotIn("bashio::config", commands)

    def test_supervisor_api_access_is_not_requested(self) -> None:
        config = (PROJECT / "ha_observer" / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("hassio_api: true", config)


if __name__ == "__main__":
    unittest.main()
