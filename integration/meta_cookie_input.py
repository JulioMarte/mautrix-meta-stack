"""Parse operator-supplied Facebook session cookies without persisting secrets."""
from __future__ import annotations

import json
import shlex
from typing import Any


class CookieInputError(ValueError):
    pass


def _cookie_header_to_dict(value: str) -> dict[str, str]:
    value = (value or "").strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()
    result: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key and val:
            result[key] = val
    return result


def _json_to_dict(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if isinstance(val, (str, int, float)) and str(val):
                out[str(key)] = str(val)
        return out
    if isinstance(value, list):
        out = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("key")
            val = item.get("value")
            if name and isinstance(val, (str, int, float)) and str(val):
                out[str(name)] = str(val)
        return out
    return {}


def _curl_to_dict(raw: str) -> dict[str, str]:
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise CookieInputError("No se pudo interpretar el cURL copiado del navegador") from exc

    result: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-b", "--cookie"} and index + 1 < len(tokens):
            result.update(_cookie_header_to_dict(tokens[index + 1]))
            index += 2
            continue
        if token in {"-H", "--header"} and index + 1 < len(tokens):
            header = tokens[index + 1]
            if header.lower().startswith("cookie:"):
                result.update(_cookie_header_to_dict(header))
            index += 2
            continue
        index += 1
    return result


def parse_cookie_input(raw: str) -> dict[str, str]:
    """Accept the same practical inputs operators copy from browser devtools.

    Supported forms:
      * JSON object: {"datr":"...","c_user":"...","sb":"...","xs":"..."}
      * JSON array exported by common cookie extensions: [{"name":"datr","value":"..."}, ...]
      * Cookie header / plain cookie string: datr=...; c_user=...; sb=...; xs=...
      * Chrome/Firefox cURL containing either -b/--cookie or a Cookie: request header
    """
    raw = (raw or "").strip()
    if not raw:
        raise CookieInputError("Pega las cookies o el cURL copiado del navegador")

    if raw.startswith("{") or raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CookieInputError("El JSON de cookies no es válido") from exc
        result = _json_to_dict(parsed)
    elif raw.lower().startswith("curl "):
        result = _curl_to_dict(raw)
    else:
        result = _cookie_header_to_dict(raw)

    if not result:
        raise CookieInputError(
            "No encontré cookies en el texto. Copia la petición como cURL (POSIX), el header Cookie o un JSON de cookies."
        )
    return result


def select_required_cookies(cookies: dict[str, str], required: list[str]) -> dict[str, str]:
    required = [str(item) for item in required if item]
    clean = {key: cookies[key] for key in required if cookies.get(key)}
    missing = [key for key in required if key not in clean]
    if missing:
        raise CookieInputError("Faltan cookies requeridas: " + ", ".join(missing))
    return clean
