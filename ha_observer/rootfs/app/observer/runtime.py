"""Application runtime and persistent log collector."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any

from .config_manager import ConfigManager
from .ha_client import HomeAssistantClient
from .store import EventStore


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class CollectorStatus:
    connected: bool = False
    events_received: int = 0
    last_event_at: str | None = None
    last_error: str | None = None


class ObserverRuntime:
    def __init__(self) -> None:
        self.retention_days = max(
            1, min(int(os.getenv("OBSERVER_RETENTION_DAYS", "30")), 365)
        )
        self.allow_sensitive_entities = _env_bool("OBSERVER_ALLOW_SENSITIVE_ENTITIES")
        self.client = HomeAssistantClient(
            os.getenv("SUPERVISOR_TOKEN", ""),
            rest_url=os.getenv("HA_REST_URL", "http://supervisor/core/api"),
            websocket_url=os.getenv(
                "HA_WEBSOCKET_URL", "ws://supervisor/core/websocket"
            ),
        )
        self.store = EventStore(
            os.getenv("OBSERVER_DB_PATH", "/data/observer.db"),
            retention_days=self.retention_days,
        )
        self.config_manager = ConfigManager(
            os.getenv("HA_CONFIG_ROOT", "/homeassistant"),
            os.getenv("CONFIG_MANAGER_DATA_PATH", "/data/config_manager"),
            enabled=_env_bool("CONFIG_MANAGEMENT_ENABLED"),
            client=self.client,
        )
        self.collector_status = CollectorStatus()
        self.collector_task: asyncio.Task[None] | None = None
        self.recovery_results: list[dict[str, str]] = []

    async def start(self) -> None:
        self.recovery_results = self.config_manager.recover_interrupted_operations()
        await self.store.initialize()
        await self.client.start()
        await self.refresh_current_errors()
        self.collector_task = asyncio.create_task(
            self.client.subscribe_system_log(self.store.upsert, self.collector_status),
            name="system-log-collector",
        )

    async def stop(self) -> None:
        if self.collector_task is not None:
            self.collector_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.collector_task
        await self.client.close()

    async def refresh_current_errors(self) -> int:
        entries = await self.client.ws_command("system_log/list")
        if not isinstance(entries, list):
            return 0
        await asyncio.gather(
            *(self.store.upsert(entry) for entry in entries if isinstance(entry, dict))
        )
        await self.store.prune()
        return len(entries)

    def status(self) -> dict[str, Any]:
        return {
            **asdict(self.collector_status),
            "retention_days": self.retention_days,
            "sensitive_entity_access": self.allow_sensitive_entities,
            "yaml_config_management": self.config_manager.status(),
            "config_recovery_results": self.recovery_results,
            "persistent_capture_note": (
                "Home Assistant must have system_log.fire_event: true for new warnings and errors "
                "to be retained as they happen. The current HA system-log buffer is also refreshed on demand."
            ),
        }
