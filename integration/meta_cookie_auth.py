"""Minimal cookie-based Meta authentication helpers.

The browser remains responsible for Facebook authentication, including MFA,
passkeys, checkpoints and other interactive challenges. This module only parses
an already-authenticated browser request and forwards the four cookies required
by mautrix-meta's Facebook cookie login flow.

Secrets are intentionally kept in memory only and must never be logged or
persisted by callers.
"""
from __future__ import annotations

import json
import re
from typing import Any


REQUIRED_FACEBOOK_COOKIES = ("datr", "c_user", "sb", "xs")


class CookieInputError(ValueError):
    """The pasted browser data does not contain a usable Facebook session."""


def _from_json(raw: str) -> dict[str, str] | None:
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        return {
            str(key): str(value)
            for key, value in data.items()
            if value is not None and not isinstance(value, (dict, list))
        }

    # Common Cookie-Editor/browser-extension export format.
    if isinstance(data, list):
        out: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if isinstance(name, str) and value is not None:
                out[name] = str(value)
        return out

    return None


def _extract_cookie_header(raw: str) -> str:
    # Chrome/Firefox Copy as cURL variants. Keep this deliberately narrow: we
    # only consume Cookie headers and never execute or otherwise interpret cURL.
    patterns = (
        r"(?:^|\s)(?:-H|--header)(?:=|\s+)\s*(['\"])(cookie\s*:\s*.*?)\1",
        r"(?:^|\s)(?:-b|--cookie)(?:=|\s+)\s*(['\"])(.*?)\1",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE | re.DOTALL)
        if match:
            value = match.group(2).strip()
            if value.lower().startswith("cookie") and ":" in value:
                value = value.split(":", 1)[1].strip()
            return value

    stripped = raw.strip()
    if stripped.lower().startswith("cookie:"):
        return stripped.split(":", 1)[1].strip()

    # Also accept a raw Cookie header value for operators who copied it from
    # DevTools directly instead of using Copy as cURL.
    if ";" in stripped and "=" in stripped and not stripped.lower().startswith("curl "):
        return stripped

    raise CookieInputError(
        "No encontré un Cookie header. Usa DevTools → Network → una petición de Facebook → Copy as cURL (POSIX), "
        "o pega un JSON de cookies."
    )


def parse_facebook_cookie_input(raw: str) -> dict[str, str]:
    """Parse Copy-as-cURL, JSON, or a raw Cookie header into required cookies."""
    raw = (raw or "").strip()
    if not raw:
        raise CookieInputError("Pega primero la información de cookies del navegador.")

    parsed = _from_json(raw)
    if parsed is None:
        header = _extract_cookie_header(raw)
        parsed = {}
        for chunk in header.split(";"):
            name, sep, value = chunk.strip().partition("=")
            if sep and name:
                parsed[name.strip()] = value.strip()

    result = {
        name: str(parsed.get(name) or "")
        for name in REQUIRED_FACEBOOK_COOKIES
        if str(parsed.get(name) or "")
    }
    missing = [name for name in REQUIRED_FACEBOOK_COOKIES if not result.get(name)]
    if missing:
        raise CookieInputError(
            "La sesión está incompleta. Faltan estas cookies requeridas por mautrix-meta: " + ", ".join(missing)
        )
    return result


def login_with_browser_cookies(client: Any, raw: str) -> dict[str, Any]:
    """Run the BridgeV2 Facebook cookie flow without persisting browser secrets."""
    cookies = parse_facebook_cookie_input(raw)
    try:
        step = client.start("facebook")
        if not isinstance(step, dict) or str(step.get("type") or "") != "cookies":
            raise RuntimeError("mautrix-meta no devolvió el paso de cookies esperado para Facebook")
        login_id = str(step.get("login_id") or "")
        step_id = str(step.get("step_id") or "")
        if not login_id or not step_id:
            raise RuntimeError("mautrix-meta devolvió un paso de cookies incompleto")
        return client.submit_cookies_trusted(
            login_id,
            step_id,
            cookies,
            txn_id=str(step.get("txn_id") or ""),
        )
    finally:
        # Best-effort lifetime reduction. Python strings cannot be reliably
        # zeroized, but we can at least drop our references immediately.
        cookies.clear()
