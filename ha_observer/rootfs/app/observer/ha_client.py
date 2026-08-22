"""Home Assistant client with read APIs plus one allowlisted config check."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import aiohttp

from .redaction import redact, redact_text

LOGGER = logging.getLogger(__name__)


class HomeAssistantAPIError(RuntimeError):
    """A sanitized Home Assistant API failure."""


class HomeAssistantClient:
    """Client limited to GET, one config-check POST, and read-only WebSocket commands."""

    READ_ONLY_WS_COMMANDS = {
        "repairs/list_issues",
        "subscribe_events",
        "system_log/list",
        "trace/get",
        "trace/list",
    }

    def __init__(
        self,
        token: str,
        *,
        rest_url: str = "http://supervisor/core/api",
        websocket_url: str = "ws://supervisor/core/websocket",
        timeout_seconds: int = 30,
    ) -> None:
        if not token:
            raise ValueError("SUPERVISOR_TOKEN is missing")
        self.token = token
        self.rest_url = rest_url.rstrip("/")
        self.websocket_url = websocket_url
        self.timeout_seconds = timeout_seconds
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"Authorization": f"Bearer {self.token}"},
            )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()

    def _require_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            raise RuntimeError("Home Assistant client has not been started")
        return self.session

    async def get_json(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        if not path.startswith("/") or ".." in path:
            raise ValueError("Invalid Home Assistant API path")
        session = self._require_session()
        async with session.get(self.rest_url + path, params=params) as response:
            if response.status >= 400:
                body = redact_text((await response.text())[:1_000])
                raise HomeAssistantAPIError(
                    f"Home Assistant GET {path} failed with HTTP {response.status}: {body}"
                )
            return await response.json(content_type=None)

    async def get_text(self, path: str) -> str:
        if not path.startswith("/") or ".." in path:
            raise ValueError("Invalid Home Assistant API path")
        session = self._require_session()
        async with session.get(self.rest_url + path) as response:
            body = await response.text()
            if response.status >= 400:
                raise HomeAssistantAPIError(
                    f"Home Assistant GET {path} failed with HTTP {response.status}: "
                    + redact_text(body[:1_000])
                )
            return redact_text(body, max_length=200_000)

    async def check_config(self) -> dict[str, Any]:
        """Run Home Assistant's admin-only configuration check."""
        path = "/config/core/check_config"
        session = self._require_session()
        async with session.post(self.rest_url + path) as response:
            body = await response.text()
            if response.status >= 400:
                raise HomeAssistantAPIError(
                    f"Home Assistant configuration check failed with HTTP {response.status}: "
                    + redact_text(body[:1_000])
                )
            try:
                result = await response.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as exc:
                raise HomeAssistantAPIError(
                    "Home Assistant configuration check returned invalid JSON"
                ) from exc
            if not isinstance(result, dict):
                raise HomeAssistantAPIError(
                    "Home Assistant configuration check returned an invalid response"
                )
            return redact(result)

    async def _authenticate_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        hello = await ws.receive_json(timeout=self.timeout_seconds)
        if hello.get("type") == "auth_required":
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json(timeout=self.timeout_seconds)
            if auth.get("type") != "auth_ok":
                raise HomeAssistantAPIError(
                    "Home Assistant WebSocket authentication failed"
                )
        elif hello.get("type") != "auth_ok":
            raise HomeAssistantAPIError("Unexpected Home Assistant WebSocket greeting")

    async def ws_command(self, command: str, **payload: Any) -> Any:
        if command not in self.READ_ONLY_WS_COMMANDS or command == "subscribe_events":
            raise ValueError(
                f"WebSocket command is not on the read-only allowlist: {command}"
            )
        session = self._require_session()
        async with session.ws_connect(self.websocket_url, heartbeat=30) as ws:
            await self._authenticate_ws(ws)
            request_id = 1
            await ws.send_json({"id": request_id, "type": command, **payload})
            while True:
                message = await ws.receive_json(timeout=self.timeout_seconds)
                if message.get("id") != request_id:
                    continue
                if message.get("type") != "result" or not message.get("success"):
                    error = redact(message.get("error", {}))
                    raise HomeAssistantAPIError(
                        f"Home Assistant {command} failed: {error}"
                    )
                return redact(message.get("result"))

    async def subscribe_system_log(self, on_entry: Any, status: Any) -> None:
        """Reconnect forever and pass sanitized system_log_event data to on_entry."""
        backoff = 1
        session = self._require_session()
        while True:
            try:
                status.connected = False
                async with session.ws_connect(self.websocket_url, heartbeat=30) as ws:
                    await self._authenticate_ws(ws)
                    request_id = 1
                    await ws.send_json(
                        {
                            "id": request_id,
                            "type": "subscribe_events",
                            "event_type": "system_log_event",
                        }
                    )
                    result = await ws.receive_json(timeout=self.timeout_seconds)
                    if not result.get("success"):
                        raise HomeAssistantAPIError(
                            "system_log_event subscription was rejected"
                        )
                    status.connected = True
                    status.last_error = None
                    backoff = 1
                    async for message in ws:
                        if message.type == aiohttp.WSMsgType.ERROR:
                            raise HomeAssistantAPIError(
                                "Home Assistant WebSocket stream failed"
                            )
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = message.json()
                        if data.get("type") != "event":
                            continue
                        event = data.get("event", {})
                        if event.get("event_type") != "system_log_event":
                            continue
                        entry = dict(event.get("data", {}))
                        entry["time_fired"] = event.get("time_fired")
                        await on_entry(redact(entry))
                        status.events_received += 1
                        status.last_event_at = event.get("time_fired")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect boundary
                status.connected = False
                status.last_error = redact_text(str(exc), max_length=500)
                LOGGER.warning(
                    "System-log collector reconnecting: %s", status.last_error
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def states(self) -> list[dict[str, Any]]:
        return redact(await self.get_json("/states"))

    async def state(self, entity_id: str) -> dict[str, Any]:
        return redact(await self.get_json("/states/" + quote(entity_id, safe=".")))

    async def history(
        self,
        entity_ids: list[str],
        *,
        hours: int,
        include_attributes: bool,
    ) -> Any:
        end = datetime.now(UTC)
        start = end - timedelta(hours=hours)
        params = {
            "filter_entity_id": ",".join(entity_ids),
            "end_time": end.isoformat(),
            "minimal_response": "true",
        }
        if not include_attributes:
            params["no_attributes"] = "true"
        path = "/history/period/" + quote(start.isoformat(), safe=":+-T")
        return redact(await self.get_json(path, params=params))

    async def logbook(self, *, hours: int, entity_id: str | None) -> Any:
        end = datetime.now(UTC)
        start = end - timedelta(hours=hours)
        params = {"end_time": end.isoformat()}
        if entity_id:
            params["entity"] = entity_id
        path = "/logbook/" + quote(start.isoformat(), safe=":+-T")
        return redact(await self.get_json(path, params=params))
