"""Minimal cookie-based Meta authentication helpers.

The browser remains responsible for Facebook authentication, including MFA,
passkeys, checkpoints and other interactive challenges. This module only parses
an already-authenticated browser request and forwards the Facebook session
cookies requested by the currently deployed mautrix-meta BridgeV2 login step.

Secrets are intentionally kept in memory only and must never be logged or
persisted by callers.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable


# Cookies known to be used by mautrix-meta Facebook web authentication across
# deployed/recent versions. The bridge's live cookie step is authoritative for
# which subset is required for a particular login attempt.
SUPPORTED_FACEBOOK_COOKIES = ("datr", "c_user", "sb", "xs")
# Preserve the old parser contract for callers/tests that invoke it directly.
REQUIRED_FACEBOOK_COOKIES = SUPPORTED_FACEBOOK_COOKIES


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


def _parse_all_cookie_input(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        raise CookieInputError("Pega primero la información de cookies del navegador.")

    parsed = _from_json(raw)
    if parsed is not None:
        return parsed

    header = _extract_cookie_header(raw)
    parsed = {}
    for chunk in header.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name:
            parsed[name.strip()] = value.strip()
    return parsed


def parse_facebook_cookie_input(
    raw: str,
    *,
    required: Iterable[str] | None = None,
) -> dict[str, str]:
    """Parse browser data and return supported Facebook cookies.

    ``required`` defaults to the historical four-cookie contract for direct
    callers. The production login path passes the exact field IDs advertised by
    the live BridgeV2 cookie step instead, so version changes in mautrix-meta do
    not make a valid browser session fail locally before provisioning sees it.
    """
    parsed = _parse_all_cookie_input(raw)
    required_names = tuple(required) if required is not None else REQUIRED_FACEBOOK_COOKIES

    unsupported = [name for name in required_names if name not in SUPPORTED_FACEBOOK_COOKIES]
    if unsupported:
        raise CookieInputError(
            "La versión desplegada de mautrix-meta solicitó cookies que este panel aún no admite: "
            + ", ".join(unsupported)
        )

    result = {
        name: str(parsed.get(name) or "")
        for name in SUPPORTED_FACEBOOK_COOKIES
        if str(parsed.get(name) or "")
    }
    missing = [name for name in required_names if not result.get(name)]
    if missing:
        raise CookieInputError(
            "La sesión está incompleta. Faltan estas cookies requeridas por mautrix-meta: " + ", ".join(missing)
        )
    return result


def _requested_cookie_names(step: dict[str, Any]) -> tuple[str, ...]:
    cookie_step = step.get("cookies")
    if not isinstance(cookie_step, dict):
        return ()
    fields = cookie_step.get("fields")
    if not isinstance(fields, list):
        return ()

    names: list[str] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = str(field.get("id") or "").strip()
        if name and bool(field.get("required", True)) and name not in names:
            names.append(name)
    return tuple(names)


def _cancel_failed_login(client: Any, login_id: str) -> None:
    """Best-effort cleanup for a BridgeV2 login process we cannot continue.

    Cleanup must never mask the original parsing/provisioning failure. Older or
    test clients may not expose ``cancel``; production ``MautrixProvisioningClient``
    does.
    """
    if not login_id:
        return
    cancel = getattr(client, "cancel", None)
    if not callable(cancel):
        return
    try:
        cancel(login_id)
    except Exception:
        pass


def login_with_browser_cookies(client: Any, raw: str) -> dict[str, Any]:
    """Run the BridgeV2 Facebook cookie flow without persisting browser secrets."""
    # Parse syntax first so malformed input doesn't create a bridge login process.
    parsed = parse_facebook_cookie_input(raw, required=())
    cookies: dict[str, str] = {}
    login_id = ""
    try:
        step = client.start("facebook")
        if not isinstance(step, dict) or str(step.get("type") or "") != "cookies":
            raise RuntimeError("mautrix-meta no devolvió el paso de cookies esperado para Facebook")
        login_id = str(step.get("login_id") or "")
        step_id = str(step.get("step_id") or "")
        if not login_id or not step_id:
            raise RuntimeError("mautrix-meta devolvió un paso de cookies incompleto")

        requested = _requested_cookie_names(step)
        if not requested:
            # Older BridgeV2 payloads did not always include the field schema.
            requested = REQUIRED_FACEBOOK_COOKIES

        unsupported = [name for name in requested if name not in SUPPORTED_FACEBOOK_COOKIES]
        if unsupported:
            raise CookieInputError(
                "La versión desplegada de mautrix-meta solicitó cookies que este panel aún no admite: "
                + ", ".join(unsupported)
            )
        missing = [name for name in requested if not parsed.get(name)]
        if missing:
            raise CookieInputError(
                "La sesión está incompleta. Faltan estas cookies requeridas por mautrix-meta: " + ", ".join(missing)
            )

        # Submit only what this bridge step requested. This both follows the
        # upstream contract and prevents unrelated browser cookies from crossing
        # the server-side provisioning boundary.
        cookies = {name: parsed[name] for name in requested}
        return client.submit_cookies_trusted(
            login_id,
            step_id,
            cookies,
            txn_id=str(step.get("txn_id") or ""),
        )
    except Exception:
        _cancel_failed_login(client, login_id)
        raise
    finally:
        # Best-effort lifetime reduction. Python strings cannot be reliably
        # zeroized, but we can at least drop our references immediately.
        parsed.clear()
        cookies.clear()
