from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
CLIENT_SOURCE = PROJECT / "ha_observer" / "rootfs" / "app" / "observer" / "ha_client.py"
SERVER_SOURCE = PROJECT / "ha_observer" / "rootfs" / "app" / "observer" / "server.py"


class HomeAssistantApiContractTests(unittest.TestCase):
    def test_client_allows_only_config_check_post(self) -> None:
        tree = ast.parse(CLIENT_SOURCE.read_text(encoding="utf-8"))
        forbidden = {"put", "patch", "delete"}
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden.isdisjoint(calls), forbidden.intersection(calls))
        self.assertIn("post", calls)
        source = CLIENT_SOURCE.read_text(encoding="utf-8")
        self.assertEqual(source.count("session.post("), 1)
        self.assertIn('path = "/config/core/check_config"', source)
        self.assertNotIn('"/services/', source)

    def test_websocket_allowlist_contains_only_read_commands(self) -> None:
        source = CLIENT_SOURCE.read_text(encoding="utf-8")
        for forbidden in (
            "call_service",
            "config_entries/flow",
            "repairs/ignore_issue",
            "trace/debug",
        ):
            self.assertNotIn(f'"{forbidden}"', source)

    def test_config_apply_and_rollback_are_annotated_destructive(self) -> None:
        source = SERVER_SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "CONFIG_APPLY = ToolAnnotations(\n    read_only_hint=False,\n    destructive_hint=True",
            source,
        )
        self.assertIn(
            '@mcp.tool(title="Apply a checked YAML change", annotations=CONFIG_APPLY)',
            source,
        )
        self.assertIn(
            '@mcp.tool(title="Roll back an applied YAML change", annotations=CONFIG_APPLY)',
            source,
        )
        for forbidden in ("call_service", "reload_all"):
            self.assertNotIn(f'"{forbidden}"', source)


if __name__ == "__main__":
    unittest.main()
