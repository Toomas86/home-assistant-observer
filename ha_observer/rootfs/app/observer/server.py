"""Read-only MCP tools for Home Assistant diagnostics."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .policy import filter_entity_ids, is_sensitive_entity, validate_entity_id, validate_trace_item_id
from .redaction import redact, redact_text
from .runtime import ObserverRuntime

runtime = ObserverRuntime()


@asynccontextmanager
async def lifespan(_: MCPServer):
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.stop()


mcp = MCPServer(
    "Home Assistant Observer",
    title="Home Assistant Observer",
    description="Read-only, privacy-redacted Home Assistant diagnostics.",
    instructions=(
        "Use these tools only to inspect Home Assistant. The server cannot call services, "
        "change entity states, edit configuration, or acknowledge repairs. Prefer targeted "
        "entity and automation queries before broad searches."
    ),
    version=__version__,
    lifespan=lifespan,
    log_level=os.getenv("OBSERVER_LOG_LEVEL", "INFO").upper(),
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _bounded(value: int, minimum: int, maximum: int, name: str) -> int:
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _concise_state(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    return {
        "entity_id": item.get("entity_id"),
        "state": item.get("state"),
        "friendly_name": attributes.get("friendly_name"),
        "device_class": attributes.get("device_class"),
        "unit_of_measurement": attributes.get("unit_of_measurement"),
        "last_changed": item.get("last_changed"),
        "last_updated": item.get("last_updated"),
    }


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok" if runtime.collector_status.connected else "degraded",
            "collector_connected": runtime.collector_status.connected,
        }
    )


@mcp.tool(title="Get Home Assistant overview", annotations=READ_ONLY)
async def ha_get_overview() -> dict[str, Any]:
    """Get HA version, entity-domain counts, unavailable entities, and collector health."""
    config, states = await asyncio.gather(
        runtime.client.get_json("/config"),
        runtime.client.states(),
    )
    visible = [
        item
        for item in states
        if isinstance(item, dict)
        and (
            runtime.allow_sensitive_entities
            or not is_sensitive_entity(str(item.get("entity_id", "unknown.unknown")))
        )
    ]
    domain_counts: dict[str, int] = {}
    unavailable: list[dict[str, Any]] = []
    for item in visible:
        entity_id = str(item.get("entity_id", "unknown.unknown"))
        domain = entity_id.split(".", 1)[0]
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if item.get("state") in {"unavailable", "unknown"} and len(unavailable) < 100:
            unavailable.append(_concise_state(item))
    return redact(
        {
            "home_assistant": {
                "version": config.get("version"),
                "state": config.get("state"),
                "safe_mode": config.get("safe_mode"),
                "time_zone": config.get("time_zone"),
                "unit_system": config.get("unit_system"),
            },
            "visible_entity_count": len(visible),
            "domain_counts": dict(sorted(domain_counts.items())),
            "unavailable_or_unknown": unavailable,
            "unavailable_or_unknown_truncated": len(unavailable) == 100,
            "observer": runtime.status(),
        }
    )


@mcp.tool(title="Get recent Home Assistant errors", annotations=READ_ONLY)
async def ha_get_recent_errors(
    limit: int = 50,
    level: Literal["WARNING", "ERROR", "CRITICAL"] | None = None,
    integration: str | None = None,
) -> dict[str, Any]:
    """Get recent sanitized warnings/errors from the current and persistent buffers."""
    limit = _bounded(limit, 1, 200, "limit")
    current_count = await runtime.refresh_current_errors()
    entries = await runtime.store.query(
        limit=limit,
        level=level,
        integration=integration.strip() if integration else None,
    )
    return {
        "entries": entries,
        "returned": len(entries),
        "current_buffer_entries_seen": current_count,
        "collector": runtime.status(),
    }


@mcp.tool(title="Search Home Assistant errors", annotations=READ_ONLY)
async def ha_search_errors(query: str, limit: int = 50) -> dict[str, Any]:
    """Search sanitized persistent and current HA warnings/errors by text."""
    query = query.strip()
    if len(query) < 2:
        raise ValueError("query must contain at least two characters")
    limit = _bounded(limit, 1, 200, "limit")
    await runtime.refresh_current_errors()
    entries = await runtime.store.query(limit=limit, search=query)
    return {"query": redact_text(query), "entries": entries, "returned": len(entries)}


@mcp.tool(title="Search raw Home Assistant error log", annotations=READ_ONLY)
async def ha_search_raw_error_log(query: str, max_lines: int = 200) -> dict[str, Any]:
    """Search HA's current-session raw error log; results are sanitized and line-limited."""
    query = query.strip()
    if len(query) < 2:
        raise ValueError("query must contain at least two characters")
    max_lines = _bounded(max_lines, 1, 500, "max_lines")
    raw = await runtime.client.get_text("/error_log")
    matches = [line for line in raw.splitlines() if query.casefold() in line.casefold()]
    return {
        "query": redact_text(query),
        "lines": matches[-max_lines:],
        "returned": min(len(matches), max_lines),
        "total_matches": len(matches),
        "truncated": len(matches) > max_lines,
    }


@mcp.tool(title="Find Home Assistant entities", annotations=READ_ONLY)
async def ha_find_entities(
    query: str | None = None,
    domain: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Find entities by ID/friendly name, optional domain, and optional exact state."""
    limit = _bounded(limit, 1, 100, "limit")
    query_folded = query.strip().casefold() if query else None
    domain_value = domain.strip().lower() if domain else None
    states = await runtime.client.states()
    matches: list[dict[str, Any]] = []
    blocked = 0
    for item in states:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id", ""))
        if is_sensitive_entity(entity_id) and not runtime.allow_sensitive_entities:
            blocked += 1
            continue
        item_domain = entity_id.split(".", 1)[0]
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        friendly_name = str(attributes.get("friendly_name", ""))
        if domain_value and item_domain != domain_value:
            continue
        if state is not None and str(item.get("state")) != state:
            continue
        if query_folded and query_folded not in entity_id.casefold() and query_folded not in friendly_name.casefold():
            continue
        matches.append(_concise_state(item))
        if len(matches) >= limit:
            break
    return {
        "entities": redact(matches),
        "returned": len(matches),
        "sensitive_entities_hidden": blocked,
    }


@mcp.tool(title="Get Home Assistant entity states", annotations=READ_ONLY)
async def ha_get_entities(entity_ids: list[str]) -> dict[str, Any]:
    """Get full sanitized states and attributes for up to 25 exact entity IDs."""
    allowed, blocked = filter_entity_ids(
        entity_ids,
        allow_sensitive=runtime.allow_sensitive_entities,
        maximum=25,
    )
    results = await asyncio.gather(
        *(runtime.client.state(entity_id) for entity_id in allowed),
        return_exceptions=True,
    )
    entities: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for entity_id, result in zip(allowed, results, strict=True):
        if isinstance(result, BaseException):
            errors.append({"entity_id": entity_id, "error": redact_text(str(result), max_length=500)})
        else:
            entities.append(redact(result))
    return {"entities": entities, "blocked_sensitive_entities": blocked, "errors": errors}


@mcp.tool(title="Get Home Assistant entity history", annotations=READ_ONLY)
async def ha_get_history(
    entity_ids: list[str],
    hours: int = 24,
    include_attributes: bool = False,
) -> dict[str, Any]:
    """Get state history for up to 10 exact entities, for at most the past 7 days."""
    hours = _bounded(hours, 1, 168, "hours")
    allowed, blocked = filter_entity_ids(
        entity_ids,
        allow_sensitive=runtime.allow_sensitive_entities,
        maximum=10,
    )
    history = (
        await runtime.client.history(
            allowed,
            hours=hours,
            include_attributes=include_attributes,
        )
        if allowed
        else []
    )
    return {"history": redact(history), "blocked_sensitive_entities": blocked, "hours": hours}


@mcp.tool(title="Get Home Assistant logbook", annotations=READ_ONLY)
async def ha_get_logbook(entity_id: str, hours: int = 24, limit: int = 200) -> dict[str, Any]:
    """Get recent logbook events for one exact entity."""
    hours = _bounded(hours, 1, 168, "hours")
    limit = _bounded(limit, 1, 500, "limit")
    entity_id = validate_entity_id(entity_id)
    if is_sensitive_entity(entity_id) and not runtime.allow_sensitive_entities:
        return {"events": [], "blocked_sensitive_entity": entity_id}
    events = await runtime.client.logbook(hours=hours, entity_id=entity_id)
    if not isinstance(events, list):
        events = []
    return {
        "entity_id": entity_id,
        "events": redact(events[-limit:]),
        "returned": min(len(events), limit),
        "total": len(events),
        "truncated": len(events) > limit,
    }


@mcp.tool(title="Get automation traces", annotations=READ_ONLY)
async def ha_get_automation_traces(
    automation_id: str,
    limit: int = 3,
    include_details: bool = True,
) -> dict[str, Any]:
    """Get recent trace summaries and optionally full traces for one automation ID."""
    item_id = validate_trace_item_id(automation_id)
    limit = _bounded(limit, 1, 5, "limit")
    summaries = await runtime.client.ws_command(
        "trace/list",
        domain="automation",
        item_id=item_id,
    )
    if not isinstance(summaries, list):
        summaries = []
    selected = summaries[-limit:][::-1]
    details: list[Any] = []
    if include_details:
        for summary in selected:
            run_id = summary.get("run_id") if isinstance(summary, dict) else None
            if not run_id:
                continue
            details.append(
                await runtime.client.ws_command(
                    "trace/get",
                    domain="automation",
                    item_id=item_id,
                    run_id=run_id,
                )
            )
    return {
        "automation_id": item_id,
        "summaries": redact(selected),
        "details": redact(details),
        "returned": len(selected),
    }


@mcp.tool(title="Get Home Assistant repairs", annotations=READ_ONLY)
async def ha_get_repairs(include_ignored: bool = False) -> dict[str, Any]:
    """List active Home Assistant Repairs issues without starting any repair flow."""
    result = await runtime.client.ws_command("repairs/list_issues")
    issues = result.get("issues", []) if isinstance(result, dict) else []
    if not include_ignored:
        issues = [issue for issue in issues if not issue.get("ignored")]
    return {"issues": redact(issues), "returned": len(issues)}


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    host="127.0.0.1",
)
