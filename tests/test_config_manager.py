from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).parents[1] / "ha_observer" / "rootfs" / "app"
sys.path.insert(0, str(APP_ROOT))

from observer.config_manager import ConfigManager  # noqa: E402


class FakeCheckClient:
    def __init__(
        self, config_file: Path, result: dict[str, object] | None = None
    ) -> None:
        self.config_file = config_file
        self.result = result or {"result": "valid", "errors": None, "warnings": None}
        self.content_seen_during_check: str | None = None
        self.error: Exception | None = None

    async def check_config(self) -> dict[str, object]:
        self.content_seen_during_check = self.config_file.read_text(encoding="utf-8")
        if self.error is not None:
            raise self.error
        return self.result


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ConfigManagerPolicyTests(unittest.TestCase):
    def test_disabled_manager_and_forbidden_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            (config / "configuration.yaml").write_text(
                "default_config:\n", encoding="utf-8"
            )
            (config / "secrets.yaml").write_text("token: secret\n", encoding="utf-8")
            client = FakeCheckClient(config / "configuration.yaml")
            disabled = ConfigManager(
                config, root / "data", enabled=False, client=client
            )
            with self.assertRaises(PermissionError):
                disabled.read_yaml("configuration.yaml", start_line=1, end_line=10)

            manager = ConfigManager(config, root / "data", enabled=True, client=client)
            for path in (
                "../secrets.yaml",
                "secrets.yaml",
                ".storage/core.config",
                "www/card.yaml",
            ):
                with (
                    self.subTest(path=path),
                    self.assertRaises((PermissionError, ValueError)),
                ):
                    manager.read_yaml(path, start_line=1, end_line=10)

    def test_read_is_redacted_but_digest_uses_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            original = "host: 192.168.1.20\ntoken: literal-secret-value\n"
            file_path = config / "configuration.yaml"
            file_path.write_text(original, encoding="utf-8")
            manager = ConfigManager(
                config,
                root / "data",
                enabled=True,
                client=FakeCheckClient(file_path),
            )

            result = manager.read_yaml("configuration.yaml", start_line=1, end_line=10)

            self.assertTrue(result["content_redacted"])
            self.assertNotIn("192.168.1.20", result["content"])
            self.assertNotIn("literal-secret-value", result["content"])
            self.assertEqual(result["sha256"], digest(original))

    def test_patch_requires_fresh_digest_and_blocks_literal_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            file_path = config / "configuration.yaml"
            original = "system_log:\n  max_entries: 50\n"
            file_path.write_text(original, encoding="utf-8")
            manager = ConfigManager(
                config,
                root / "data",
                enabled=True,
                client=FakeCheckClient(file_path),
            )

            with self.assertRaises(RuntimeError):
                manager.stage_patch(
                    "configuration.yaml",
                    expected_sha256="0" * 64,
                    old_text="max_entries: 50",
                    new_text="max_entries: 100",
                )
            with self.assertRaises(PermissionError):
                manager.stage_append(
                    "configuration.yaml",
                    expected_sha256=digest(original),
                    content="api_key: do-not-store-this-here\n",
                )


class ConfigManagerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_checked_apply_and_rollback_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            file_path = config / "configuration.yaml"
            original = "system_log:\n  max_entries: 50\n"
            candidate = "system_log:\n  max_entries: 100\n  fire_event: true\n"
            file_path.write_text(original, encoding="utf-8")
            client = FakeCheckClient(file_path)
            manager = ConfigManager(config, root / "data", enabled=True, client=client)

            staged = manager.stage_patch(
                "configuration.yaml",
                expected_sha256=digest(original),
                old_text="  max_entries: 50\n",
                new_text="  max_entries: 100\n  fire_event: true\n",
            )
            self.assertEqual(file_path.read_text(encoding="utf-8"), original)
            self.assertIn("fire_event", staged["diff"])

            checked = await manager.check(staged["change_id"])
            self.assertEqual(client.content_seen_during_check, candidate)
            self.assertEqual(file_path.read_text(encoding="utf-8"), original)
            self.assertTrue(checked["original_restored_after_check"])
            self.assertEqual(checked["home_assistant_result"], "valid")

            with self.assertRaises(PermissionError):
                await manager.apply(staged["change_id"], approval_code="wrong")
            applied = await manager.apply(
                staged["change_id"], approval_code=str(checked["approval_code"])
            )
            self.assertEqual(file_path.read_text(encoding="utf-8"), candidate)
            self.assertFalse(applied["reloaded_or_restarted"])

            with self.assertRaises(PermissionError):
                await manager.rollback(staged["change_id"], rollback_code="wrong")
            rolled_back = await manager.rollback(
                staged["change_id"], rollback_code=str(applied["rollback_code"])
            )
            self.assertEqual(file_path.read_text(encoding="utf-8"), original)
            self.assertEqual(rolled_back["status"], "rolled_back")

    async def test_invalid_home_assistant_check_restores_original(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            file_path = config / "scripts.yaml"
            original = "hello:\n  sequence: []\n"
            file_path.write_text(original, encoding="utf-8")
            client = FakeCheckClient(
                file_path,
                {"result": "invalid", "errors": "bad script", "warnings": None},
            )
            manager = ConfigManager(config, root / "data", enabled=True, client=client)
            staged = manager.stage_append(
                "scripts.yaml",
                expected_sha256=digest(original),
                content="broken:\n  sequence: []\n",
            )

            checked = await manager.check(staged["change_id"])

            self.assertEqual(checked["status"], "invalid")
            self.assertNotIn("approval_code", checked)
            self.assertEqual(file_path.read_text(encoding="utf-8"), original)

    async def test_create_file_can_be_checked_applied_and_removed_by_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            automations = config / "automations"
            automations.mkdir(parents=True)
            file_path = automations / "test.yaml"
            client = FakeCheckClient(file_path)
            manager = ConfigManager(config, root / "data", enabled=True, client=client)
            content = "- alias: Safe test\n  triggers: []\n  actions: []\n"
            staged = manager.stage_create("automations/test.yaml", content=content)

            checked = await manager.check(staged["change_id"])
            self.assertFalse(file_path.exists())
            applied = await manager.apply(
                staged["change_id"], approval_code=str(checked["approval_code"])
            )
            self.assertEqual(file_path.read_text(encoding="utf-8"), content)
            await manager.rollback(
                staged["change_id"], rollback_code=str(applied["rollback_code"])
            )
            self.assertFalse(file_path.exists())

    async def test_external_change_prevents_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            file_path = config / "configuration.yaml"
            original = "default_config:\n"
            file_path.write_text(original, encoding="utf-8")
            client = FakeCheckClient(file_path)
            manager = ConfigManager(config, root / "data", enabled=True, client=client)
            staged = manager.stage_append(
                "configuration.yaml",
                expected_sha256=digest(original),
                content="system_log:\n  fire_event: true\n",
            )
            checked = await manager.check(staged["change_id"])
            file_path.write_text(
                "default_config:\n# changed elsewhere\n", encoding="utf-8"
            )

            with self.assertRaises(RuntimeError):
                await manager.apply(
                    staged["change_id"], approval_code=str(checked["approval_code"])
                )

    async def test_interrupted_check_is_recovered_from_protected_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            file_path = config / "configuration.yaml"
            original = "default_config:\n"
            candidate = "default_config:\nsystem_log:\n  fire_event: true\n"
            file_path.write_text(original, encoding="utf-8")
            manager = ConfigManager(
                config,
                root / "data",
                enabled=True,
                client=FakeCheckClient(file_path),
            )
            staged = manager.stage_append(
                "configuration.yaml",
                expected_sha256=digest(original),
                content="system_log:\n  fire_event: true\n",
            )
            change = manager._load_change(staged["change_id"])
            target, original_bytes, candidate_bytes, mode = manager._candidate(change)
            manager._ensure_backup(change, original_bytes=original_bytes, mode=mode)
            change["status"] = "checking"
            manager._save_change(change)
            target.write_text(candidate, encoding="utf-8")

            result = manager.recover_interrupted_operations()

            self.assertEqual(result[0]["status"], "restored")
            self.assertEqual(file_path.read_text(encoding="utf-8"), original)
            metadata = json.loads(
                (root / "data" / "changes" / f"{staged['change_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["status"], "check_failed")

    async def test_interrupted_apply_and_rollback_are_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = root / "config"
            config.mkdir()
            file_path = config / "configuration.yaml"
            original = "default_config:\n"
            candidate = "default_config:\nsystem_log:\n  fire_event: true\n"
            file_path.write_text(original, encoding="utf-8")
            manager = ConfigManager(
                config,
                root / "data",
                enabled=True,
                client=FakeCheckClient(file_path),
            )
            staged = manager.stage_append(
                "configuration.yaml",
                expected_sha256=digest(original),
                content="system_log:\n  fire_event: true\n",
            )
            checked = await manager.check(staged["change_id"])
            self.assertIn("approval_code", checked)

            change = manager._load_change(staged["change_id"])
            change["status"] = "applying"
            manager._save_change(change)
            file_path.write_text(candidate, encoding="utf-8")
            recovered_apply = manager.recover_interrupted_operations()
            self.assertEqual(recovered_apply[0]["status"], "applied")
            applied_change = manager._load_change(staged["change_id"])
            self.assertEqual(applied_change["status"], "applied")
            self.assertIn("rollback_code", applied_change)

            applied_change["status"] = "rolling_back"
            manager._save_change(applied_change)
            file_path.write_text(original, encoding="utf-8")
            recovered_rollback = manager.recover_interrupted_operations()
            self.assertEqual(recovered_rollback[0]["status"], "rolled_back")
            rolled_back_change = manager._load_change(staged["change_id"])
            self.assertEqual(rolled_back_change["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()
