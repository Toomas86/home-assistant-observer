"""Privacy-preserving redaction for Home Assistant diagnostic data."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:access_?token|api_?key|authorization|bearer|client_?secret|"
    r"credential|latitude|longitude|oauth|passcode|password|pin|refresh_?token|"
    r"secret|token)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[ _-]?key|authorization|bearer|client[ _-]?secret|password|"
    r"refresh[ _-]?token|secret|token)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_URL_SECRET = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|auth|key|password|secret|token)=)[^&#\s]+"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6_CANDIDATE = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")
_MAC = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])")


def _redact_ipv4(match: re.Match[str]) -> str:
    candidate = match.group(0)
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return candidate
    return "[REDACTED_IP]"


def _redact_ipv6(match: re.Match[str]) -> str:
    candidate = match.group(0)
    try:
        if ipaddress.ip_address(candidate).version != 6:
            return candidate
    except ValueError:
        return candidate
    return "[REDACTED_IP]"


def redact_text(value: str, *, max_length: int = 20_000) -> str:
    """Redact credentials and network identifiers from a string."""
    value = _BEARER.sub("Bearer " + REDACTED, value)
    value = _OPENAI_KEY.sub(REDACTED, value)
    value = _JWT.sub(REDACTED, value)
    value = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", value)
    value = _URL_SECRET.sub(lambda m: m.group(1) + REDACTED, value)
    value = _MAC.sub("[REDACTED_MAC]", value)
    value = _IPV4.sub(_redact_ipv4, value)
    value = _IPV6_CANDIDATE.sub(_redact_ipv6, value)
    if len(value) > max_length:
        return value[:max_length] + f"\n...[truncated {len(value) - max_length} characters]"
    return value


def redact(value: Any, *, max_depth: int = 12, max_items: int = 500, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like value and bound its output size."""
    if _depth >= max_depth:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= max_items:
                result["_truncated"] = f"{len(value) - max_items} more fields"
                break
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text):
                result[key_text] = REDACTED
            else:
                result[key_text] = redact(
                    child,
                    max_depth=max_depth,
                    max_items=max_items,
                    _depth=_depth + 1,
                )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [
            redact(item, max_depth=max_depth, max_items=max_items, _depth=_depth + 1)
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            items.append(f"[TRUNCATED {len(value) - max_items} ITEMS]")
        return items
    if isinstance(value, bytes):
        return "[BINARY DATA]"
    return value
