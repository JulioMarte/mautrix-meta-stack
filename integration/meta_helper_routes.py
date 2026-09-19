"""HTTP boundary used by the trusted desktop Meta authentication helper."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from meta_helper_handoff import HandoffError, registry
from meta_onboarding_diagnostics import event, new_trace_id
from meta_provisioning import ProvisioningError


MAX_BODY_BYTES = 32768
MAX_COOKIE_VALUE_BYTES = 8192


def create_pairing(safe_step: dict[str, Any]) -> dict[str, Any]:
    item, token = registry.create(safe_step)
    event(
        "helper_pairing_created",
        handoff_id=item.handoff_id,
        login_id=item.login_id,
        step_id=item.step_id,
        step_type=item.metadata.get("type", ""),
        expires_at=int(item.expires_at),
    )
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
        trace_id = request.headers.get("x-meta-trace-id", "")[:64] or new_trace_id()
        try:
            item = registry.get(handoff_id, _bearer(request), consume=False)
        except HandoffError:
            event("helper_descriptor_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="invalid_or_expired")
            return _json({"error": "Invalid or expired helper pairing", "trace_id": trace_id}, 401)
        event(
            "helper_descriptor_served",
            trace_id=trace_id,
            handoff_id=handoff_id,
            login_id=item.login_id,
            step_id=item.step_id,
            step_type=item.metadata.get("type", ""),
        )
        return _json({
            "type": "meta-cookie-login",
            "expires_at": int(item.expires_at),
            "step": item.metadata,
            "trace_id": trace_id,
        })

    @app.post("/api/meta/helper/{handoff_id}")
    async def submit_meta_helper_handoff(handoff_id: str, request: Request):
        trace_id = request.headers.get("x-meta-trace-id", "")[:64] or new_trace_id()
        content_length = request.headers.get("content-length")
        try:
            if content_length and int(content_length) > MAX_BODY_BYTES:
                event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="payload_too_large")
                return _json({"error": "Helper payload too large", "trace_id": trace_id}, 413)
        except ValueError:
            event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="invalid_content_length")
            return _json({"error": "Invalid content length", "trace_id": trace_id}, 400)

        token = _bearer(request)
        try:
            item = registry.get(handoff_id, token, consume=False)
        except HandoffError:
            event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="invalid_expired_or_used")
            return _json({"error": "Invalid, expired, or already used helper pairing", "trace_id": trace_id}, 401)

        try:
            payload = await request.json()
        except Exception:
            event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="invalid_json")
            return _json({"error": "Invalid JSON payload", "trace_id": trace_id}, 400)
        values = None
        if isinstance(payload, dict):
            values = payload.get("values")
            if values is None:
                values = payload.get("cookies")  # backward-compatible helper payload
        if not isinstance(values, dict):
            event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="missing_value_object")
            return _json({"error": "Authentication field payload is required", "trace_id": trace_id}, 400)

        allowed = set(_required_cookie_ids(item.metadata))
        clean: dict[str, str] = {}
        for key, value in values.items():
            key = str(key)
            if key not in allowed or not isinstance(value, str):
                continue
            if not value or len(value.encode("utf-8")) > MAX_COOKIE_VALUE_BYTES:
                event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="invalid_cookie_value", field=key)
                return _json({"error": f"Invalid cookie value for {key}", "trace_id": trace_id}, 400)
            clean[key] = value
        missing = sorted(allowed - set(clean))
        if missing:
            event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="required_fields_missing", missing=missing)
            return _json({"error": "Required Meta authentication fields were not captured", "missing": missing, "trace_id": trace_id}, 400)

        try:
            item = registry.get(handoff_id, token, consume=True)
        except HandoffError:
            clean.clear()
            values.clear()
            event("helper_submission_rejected", trace_id=trace_id, handoff_id=handoff_id, reason="replay_race")
            return _json({"error": "Invalid, expired, or already used helper pairing", "trace_id": trace_id}, 401)

        event(
            "helper_submission_forwarding",
            trace_id=trace_id,
            handoff_id=handoff_id,
            login_id=item.login_id,
            step_id=item.step_id,
            fields=sorted(clean.keys()),
        )
        try:
            next_step = client_factory().submit_cookies_trusted(
                item.login_id,
                item.step_id,
                clean,
                txn_id=item.txn_id,
            )
            safe = store_step(next_step)
        except ProvisioningError as exc:
            event(
                "helper_submission_failed",
                trace_id=trace_id,
                handoff_id=handoff_id,
                error_type="provisioning",
                errcode=exc.errcode,
                status_code=exc.status_code,
            )
            return _json({"error": str(exc), "trace_id": trace_id}, 400)
        except Exception as exc:
            event(
                "helper_submission_failed",
                trace_id=trace_id,
                handoff_id=handoff_id,
                error_type=type(exc).__name__,
            )
            return _json({"error": "Meta authentication could not be completed", "trace_id": trace_id}, 400)
        finally:
            clean.clear()
            values.clear()

        event(
            "helper_submission_complete",
            trace_id=trace_id,
            handoff_id=handoff_id,
            complete=safe.get("type") == "complete",
            next_step=safe.get("type") or "",
        )
        return _json({
            "ok": True,
            "complete": safe.get("type") == "complete",
            "next_step": safe.get("type") or "",
            "trace_id": trace_id,
        })
