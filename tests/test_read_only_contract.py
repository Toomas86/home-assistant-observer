from __future__ import annotations

import ast
import unittest
from pathlib import Path

PROJECT = Path(__file__).parents[1]
CLIENT_SOURCE = PROJECT / "ha_observer" / "rootfs" / "app" / "observer" / "ha_client.py"


class ReadOnlyContractTests(unittest.TestCase):
    def test_client_has_no_mutating_http_calls(self) -> None:
        tree = ast.parse(CLIENT_SOURCE.read_text(encoding="utf-8"))
        forbidden = {"post", "put", "patch", "delete"}
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(forbidden.isdisjoint(calls), forbidden.intersection(calls))

    def test_websocket_allowlist_contains_only_read_commands(self) -> None:
        source = CLIENT_SOURCE.read_text(encoding="utf-8")
        for forbidden in (
            "call_service",
            "config_entries/flow",
            "repairs/ignore_issue",
            "trace/debug",
        ):
            self.assertNotIn(f'"{forbidden}"', source)


if __name__ == "__main__":
    unittest.main()
