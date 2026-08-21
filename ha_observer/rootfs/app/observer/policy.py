"""Read-only access policy and input validation."""

from __future__ import annotations

import re

SENSITIVE_ENTITY_DOMAINS = {
    "camera",
    "device_tracker",
    "geo_location",
    "image",
    "person",
}

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_TRACE_ITEM_ID = re.compile(r"^[a-zA-Z0-9_.-]+$")


def validate_entity_id(entity_id: str) -> str:
    value = entity_id.strip().lower()
    if not _ENTITY_ID.fullmatch(value):
        raise ValueError(f"Invalid Home Assistant entity_id: {entity_id!r}")
    return value


def validate_trace_item_id(item_id: str) -> str:
    value = item_id.strip()
    if value.startswith("automation."):
        value = value.split(".", 1)[1]
    if not value or not _TRACE_ITEM_ID.fullmatch(value):
        raise ValueError(f"Invalid automation id: {item_id!r}")
    return value


def is_sensitive_entity(entity_id: str) -> bool:
    return entity_id.split(".", 1)[0] in SENSITIVE_ENTITY_DOMAINS


def filter_entity_ids(entity_ids: list[str], *, allow_sensitive: bool, maximum: int = 25) -> tuple[list[str], list[str]]:
    if not entity_ids:
        raise ValueError("At least one entity_id is required")
    if len(entity_ids) > maximum:
        raise ValueError(f"At most {maximum} entity IDs are allowed per call")

    allowed: list[str] = []
    blocked: list[str] = []
    for raw in entity_ids:
        entity_id = validate_entity_id(raw)
        if is_sensitive_entity(entity_id) and not allow_sensitive:
            blocked.append(entity_id)
        elif entity_id not in allowed:
            allowed.append(entity_id)
    return allowed, blocked
