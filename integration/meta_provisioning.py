"""Private server-side adapter for mautrix BridgeV2 provisioning.

This module deliberately has no web routes. It is consumed by the authenticated
NiceGUI admin surface and trusted local auth helper. The mautrix provisioning
shared secret never needs to reach browser JavaScript.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
import yaml


DEFAULT_BASE_URL = "http://mautrix-meta:29319/_matrix/provision"
DEFAULT_SECRET_PATH = "/run/mautrix-provisioning/shared_secret"
DEFAULT_CONFIG_PATH = "/mautrix/config.yaml"
DEFAULT_TIMEOUT = 15


class ProvisioningError(RuntimeError):
    """A normalized mautrix provisioning failure safe to show to an operator."""

    def __init__(self, message: str, *, errcode: str = "", status_code: int = 0):
        super().__init__(message)
        self.errcode = errcode
        self.status_code = status_code


@dataclass(frozen=True)
class ProvisioningConfig:
    base_url: str
    user_id: str
    shared_secret: str
    timeout: int = DEFAULT_TIMEOUT


def _validate_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Invalid mautrix provisioning URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Mautrix provisioning URL must not contain credentials, query or fragment")
    return value


def _validate_secret(secret: str) -> str:
    secret = (secret or "").strip()
    if secret in {"", "generate", "disable"} or len(secret) < 16:
        raise ProvisioningError("Mautrix provisioning is not initialized with a usable shared secret")
    return secret


def load_shared_secret(config_path: str = DEFAULT_CONFIG_PATH) -> str:
    """Load the provisioning secret through the narrowest available boundary.

    Production Compose extracts only ``provisioning.shared_secret`` into a small
    private handoff volume before mautrix starts. This avoids granting the
    integration sidecar read access to mautrix's complete runtime config, which
    mautrix v26.08.1 rewrites to its own UID/GID and mode 0600 on startup.

    Precedence:
      1. explicit environment value (tests/special deployments),
      2. isolated secret file (normal production path),
      3. legacy config.yaml fallback for backwards compatibility only.
    """
    env_secret = os.getenv("MAUTRIX_PROVISIONING_SECRET", "").strip()
    if env_secret:
        return _validate_secret(env_secret)

    secret_path = Path(os.getenv("MAUTRIX_PROVISIONING_SECRET_PATH", DEFAULT_SECRET_PATH))
    try:
        if secret_path.is_file():
            return _validate_secret(secret_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProvisioningError("Could not read the private mautrix provisioning secret") from exc

    path = Path(os.getenv("MAUTRIX_CONFIG_PATH", config_path))
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProvisioningError("Could not read the private mautrix provisioning secret") from exc
    return _validate_secret(str((payload.get("provisioning") or {}).get("shared_secret") or ""))


def default_config() -> ProvisioningConfig:
    user_id = os.getenv("MATRIX_ADMIN_MXID", "").strip()
    if not user_id.startswith("@") or ":" not in user_id:
        raise ProvisioningError("MATRIX_ADMIN_MXID is not configured correctly")
    return ProvisioningConfig(
        base_url=_validate_base_url(os.getenv("MAUTRIX_PROVISIONING_URL", DEFAULT_BASE_URL)),
        user_id=user_id,
        shared_secret=load_shared_secret(),
        timeout=int(os.getenv("MAUTRIX_PROVISIONING_TIMEOUT", str(DEFAULT_TIMEOUT))),
    )


def safe_step(step: dict[str, Any] | None) -> dict[str, Any]:
    """Return only login-step metadata that is safe to persist/render."""
    if not isinstance(step, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("login_id", "type", "step_id", "txn_id", "instructions"):
        value = step.get(key)
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            out[key] = value

    if isinstance(step.get("cookies"), dict):
        cookies = step["cookies"]
        out["cookies"] = {
            key: cookies[key]
            for key in ("url", "user_agent", "wait_for_url_pattern")
            if isinstance(cookies.get(key), str)
        }
        fields = []
        for item in cookies.get("fields") or []:
            if isinstance(item, dict):
                fields.append({
                    key: item[key]
                    for key in ("id", "name", "required")
                    if isinstance(item.get(key), (str, bool))
                })
        if fields:
            out["cookies"]["fields"] = fields

    if isinstance(step.get("user_input"), dict):
        params = step["user_input"]
        fields = []
        for item in params.get("fields") or []:
            if not isinstance(item, dict):
                continue
            safe = {
                key: item[key]
                for key in ("id", "name", "description", "type", "required", "pattern")
                if isinstance(item.get(key), (str, bool))
            }
            if isinstance(item.get("options"), list):
                normalized_options: list[str | dict[str, str]] = []
                for option in item["options"]:
                    if isinstance(option, str):
                        normalized_options.append(option)
                    elif isinstance(option, dict):
                        normalized = {
                            key: option[key]
                            for key in ("id", "name")
                            if isinstance(option.get(key), str)
                        }
                        if normalized:
                            normalized_options.append(normalized)
                safe["options"] = normalized_options
            fields.append(safe)
        out["user_input"] = {"fields": fields}

    if isinstance(step.get("display_and_wait"), dict):
        display = step["display_and_wait"]
        out["display_and_wait"] = {
            key: display[key]
            for key in ("type", "data", "image_url")
            if isinstance(display.get(key), str)
        }

    return out


class MautrixProvisioningClient:
    def __init__(self, config: ProvisioningConfig | None = None, session: requests.Session | None = None):
        self.config = config or default_config()
        self.session = session or requests.Session()
        self.session.trust_env = False

    def _request(self, method: str, path: str, *, payload: Any = None, params: dict[str, Any] | None = None) -> Any:
        query = {"user_id": self.config.user_id}
        if params:
            query.update({k: v for k, v in params.items() if v is not None and v != ""})
        headers = {
            "Authorization": f"Bearer {self.config.shared_secret}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.session.request(
                method,
                f"{self.config.base_url}{path}",
                params=query,
                json=payload,
                headers=headers,
                timeout=self.config.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ProvisioningError("Could not reach the private mautrix provisioning API") from exc

        if 300 <= response.status_code < 400:
            raise ProvisioningError("Mautrix provisioning unexpectedly attempted an HTTP redirect", status_code=response.status_code)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            errcode = str(body.get("errcode") or "") if isinstance(body, dict) else ""
            message = str(body.get("error") or "") if isinstance(body, dict) else ""
            if not message:
                message = f"Mautrix provisioning returned HTTP {response.status_code}"
            raise ProvisioningError(message, errcode=errcode, status_code=response.status_code)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProvisioningError("Mautrix provisioning returned invalid JSON") from exc

    def whoami(self) -> dict[str, Any]:
        data = self._request("GET", "/v3/whoami")
        return data if isinstance(data, dict) else {}

    def flows(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v3/login/flows")
        flows = data.get("flows") if isinstance(data, dict) else None
        return [item for item in (flows or []) if isinstance(item, dict)]

    def logins(self) -> list[str]:
        data = self._request("GET", "/v3/logins")
        ids = data.get("login_ids") if isinstance(data, dict) else None
        return [str(item) for item in (ids or [])]

    def start(self, flow_id: str, *, existing_login_id: str = "") -> dict[str, Any]:
        flow_id = (flow_id or "").strip()
        if not flow_id:
            raise ValueError("A Meta login flow must be selected")
        data = self._request(
            "POST",
            f"/v3/login/start/{quote(flow_id, safe='')}",
            params={"login_id": existing_login_id or None},
        )
        if not isinstance(data, dict):
            raise ProvisioningError("Mautrix returned an invalid login step")
        return data

    def submit_user_input(self, login_id: str, step_id: str, values: dict[str, str], *, txn_id: str = "") -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/v3/login/step/{quote(login_id, safe='')}/{quote(step_id, safe='')}/user_input",
            params={"txn_id": txn_id or None},
            payload={str(k): str(v) for k, v in values.items()},
        )
        return data if isinstance(data, dict) else {}

    def submit_cookies_trusted(self, login_id: str, step_id: str, cookies: dict[str, str], *, txn_id: str = "") -> dict[str, Any]:
        """Trusted-helper boundary. Do not expose this as a normal browser form."""
        data = self._request(
            "POST",
            f"/v3/login/step/{quote(login_id, safe='')}/{quote(step_id, safe='')}/cookies",
            params={"txn_id": txn_id or None},
            payload={str(k): str(v) for k, v in cookies.items()},
        )
        return data if isinstance(data, dict) else {}

    def wait(self, login_id: str, step_id: str, *, txn_id: str = "") -> dict[str, Any]:
        data = self._request(
            "POST",
            f"/v3/login/step/{quote(login_id, safe='')}/{quote(step_id, safe='')}/display_and_wait",
            params={"txn_id": txn_id or None},
        )
        return data if isinstance(data, dict) else {}

    def cancel(self, login_id: str) -> None:
        self._request("POST", f"/v3/login/cancel/{quote(login_id, safe='')}")

    def logout(self, login_id: str = "all") -> None:
        self._request("POST", f"/v3/logout/{quote(login_id or 'all', safe='')}")


def connection_summary(whoami: dict[str, Any]) -> dict[str, Any]:
    """Normalize BridgeV2 whoami into product-facing state."""
    logins = whoami.get("logins") if isinstance(whoami, dict) else None
    logins = [item for item in (logins or []) if isinstance(item, dict)]
    if not logins:
        return {"status": "disconnected", "connected": False, "logins": []}

    normalized = []
    any_connected = False
    any_bad = False
    for item in logins:
        state = item.get("state")
        if isinstance(state, dict):
            event = str(state.get("state_event") or state.get("event") or "")
            reason = str(state.get("message") or state.get("reason") or "")
        else:
            event = str(item.get("state_event") or state or "")
            reason = str(item.get("state_reason") or "")
        lowered = event.lower()
        connected = "connected" in lowered and "disconnected" not in lowered
        bad = any(token in lowered for token in ("bad", "error", "unknown", "logged_out", "disconnected"))
        any_connected = any_connected or connected
        any_bad = any_bad or bad
        normalized.append({
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or (item.get("profile") or {}).get("name") or "Meta account"),
            "event": event,
            "reason": reason,
            "connected": connected,
        })

    if any_connected:
        status = "connected"
    elif any_bad:
        status = "action_required"
    else:
        status = "connecting"
    return {"status": status, "connected": any_connected, "logins": normalized}