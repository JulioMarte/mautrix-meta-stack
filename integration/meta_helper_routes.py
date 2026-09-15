"""HTTP boundary used by the trusted desktop Meta authentication helper."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from meta_helper_handoff import HandoffError, registry


MAX_BODY_BYTES = 32768
MAX_COOKIE_VALUE_BYTES = 8192


def create_pairing(safe_step: dict[str, Any]) -> dict[str, Any]:
    item, token = registry.create(safe_step)
    return {
        "id": item.handoff_id,
        "token": token,
        "expires_at": int(item.expires_at),
    }


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    response = JSONResponse(data, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _required_cookie_ids(metadata: dict[str, Any]) -> list[str]:
    fields = (metadata.get("cookies") or {}).get("fields") or []
    return [
        str(field.get("id"))
        for field in fields
        if isinstance(field, dict) and field.get("id") and field.get("required", True)
    ]


def register_helper_routes(app, client_factory: Callable[[], Any], store_step: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Register one-time helper routes on the NiceGUI/FastAPI app."""

    @app.get("/api/meta/helper/{handoff_id}")
    async def get_meta_helper_handoff(handoff_id: str, request: Request):
        try:
            item = registry.get(handoff_id, _bearer(request), consume=False)
        except HandoffError:
            return _json({"error": "Invalid or expired helper pairing"}, 401)
        return _json({
            "type": "meta-cookie-login",
            "expires_at": int(item.expires_at),
            "step": item.metadata,
        })

    @app.post("/api/meta/helper/{handoff_id}")
    async def submit_meta_helper_handoff(handoff_id: str, request: Request):
        content_length = request.headers.get("content-length")
        try:
            if content_length and int(content_length) > MAX_BODY_BYTES:
                return _json({"error": "Helper payload too large"}, 413)
        except ValueError:
            return _json({"error": "Invalid content length"}, 400)

        token = _bearer(request)
        try:
            item = registry.get(handoff_id, token, consume=True)
        except HandoffError:
            return _json({"error": "Invalid, expired, or already used helper pairing"}, 401)

        try:
            payload = await request.json()
        except Exception:
            return _json({"error": "Invalid JSON payload"}, 400)
        cookies = payload.get("cookies") if isinstance(payload, dict) else None
        if not isinstance(cookies, dict):
            return _json({"error": "Cookie payload is required"}, 400)

        allowed = set(_required_cookie_ids(item.metadata))
        clean: dict[str, str] = {}
        for key, value in cookies.items():
            key = str(key)
            if key not in allowed or not isinstance(value, str):
                continue
            if not value or len(value.encode("utf-8")) > MAX_COOKIE_VALUE_BYTES:
                return _json({"error": f"Invalid cookie value for {key}"}, 400)
            clean[key] = value
        missing = sorted(allowed - set(clean))
        if missing:
            return _json({"error": "Required Meta cookies were not captured", "missing": missing}, 400)

        try:
            next_step = client_factory().submit_cookies_trusted(
                item.login_id,
                item.step_id,
                clean,
                txn_id=item.txn_id,
            )
            safe = store_step(next_step)
        except Exception as exc:
            # Pairing is intentionally spent even when Meta rejects the submitted
            # session.  The operator can issue a fresh short-lived pairing without
            # replaying the previous cookie payload.
            return _json({"error": str(exc)}, 400)
        finally:
            # Drop local references to raw cookie values as soon as the synchronous
            # provisioning submission has returned.
            clean.clear()
            cookies.clear()

        return _json({
            "ok": True,
            "complete": safe.get("type") == "complete",
            "next_step": safe.get("type") or "",
        })
