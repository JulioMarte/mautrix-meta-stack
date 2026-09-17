"""Structured, secret-safe diagnostics for managed Meta onboarding.

The goal is operational visibility inside the integration container without ever
logging raw authentication material. Events are emitted as one-line JSON so they
can be correlated easily in Docker/Coolify logs.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any


logger = logging.getLogger("meta_onboarding")

_SENSITIVE_MARKERS = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
    "assertion",
    "credential",
    "xs",
    "datr",
    "c_user",
    "sb",
)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def _safe_key(key: str) -> bool:
    lowered = key.lower()
    return not any(marker in lowered for marker in _SENSITIVE_MARKERS)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Return a bounded representation that excludes likely authentication data."""
    if depth > 4:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:300]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key = str(key)
            if not _safe_key(key):
                out[key] = "<redacted>"
            else:
                out[key] = sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item, depth=depth + 1) for item in list(value)[:50]]
    return str(value)[:300]


def event(name: str, *, trace_id: str = "", level: int = logging.INFO, **fields: Any) -> None:
    payload = {
        "ts": round(time.time(), 3),
        "component": "meta_onboarding",
        "event": str(name),
        "trace_id": trace_id or "",
    }
    payload.update(sanitize(fields))
    logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
