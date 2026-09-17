"""Recovery helpers for short-lived BridgeV2 login processes.

BridgeV2 login process IDs are runtime state, not durable account IDs. The
integration UI may outlive a mautrix-meta restart because it persists sanitized
step metadata in SQLite. This module classifies the specific 404 returned when a
saved step points at a process the bridge no longer knows about.
"""
from __future__ import annotations

from meta_provisioning import ProvisioningError


def is_missing_login_process(exc: Exception) -> bool:
    """Return True only for a lost/expired BridgeV2 login process."""
    if not isinstance(exc, ProvisioningError) or exc.status_code != 404:
        return False

    errcode = (exc.errcode or "").strip().upper()
    message = str(exc).strip().lower()
    return "login not found" in message or (errcode == "M_NOT_FOUND" and "login" in message)
