from __future__ import annotations

import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
RUN_SCRIPT = PROJECT / "ha_observer" / "run.sh"


class AppStartupTests(unittest.TestCase):
    def test_s6_environment_is_imported(self) -> None:
        source = RUN_SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(source.startswith("#!/usr/bin/with-contenv bashio\n"))

    def test_options_are_read_from_mounted_file(self) -> None:
        source = RUN_SCRIPT.read_text(encoding="utf-8")
        commands = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn('options_file="/data/options.json"', source)
        self.assertIn("jq --raw-output '.tunnel_id // empty'", source)
        self.assertIn("jq --raw-output '.openai_runtime_api_key // empty'", source)
        self.assertIn("jq --raw-output '.config_management_enabled // false'", source)
        self.assertNotIn("bashio::config", commands)

    def test_supervisor_api_access_is_not_requested(self) -> None:
        config = (PROJECT / "ha_observer" / "config.yaml").read_text(encoding="utf-8")
        self.assertNotIn("hassio_api: true", config)

    def test_config_management_is_opt_in_and_uses_only_ha_config_mount(self) -> None:
        config = (PROJECT / "ha_observer" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("config_management_enabled: false", config)
        self.assertIn("type: homeassistant_config", config)
        self.assertIn("path: /homeassistant", config)
        for forbidden in (
            "docker_api: true",
            "full_access: true",
            "host_network: true",
        ):
            self.assertNotIn(forbidden, config)


if __name__ == "__main__":
    unittest.main()
