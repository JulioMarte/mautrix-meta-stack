"""Ephemeral pairing state for the trusted Meta authentication helper.

No cookie values or raw pairing tokens are persisted.  The registry is process
memory only and is intentionally short-lived.  A restart simply invalidates any
in-progress helper pairing and the operator can issue another pairing session.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import secrets
import threading
import time
from typing import Any


DEFAULT_TTL = 5 * 60


@dataclass
class Handoff:
    handoff_id: str
    token_digest: str
    created_at: float
    expires_at: float
    login_id: str
    step_id: str
    txn_id: str
    metadata: dict[str, Any]
    used_at: float | None = None


class HandoffError(RuntimeError):
    pass


class HandoffRegistry:
    def __init__(self, ttl: int = DEFAULT_TTL):
        self.ttl = ttl
        self._items: dict[str, Handoff] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _purge_locked(self, now: float) -> None:
        stale = [
            key for key, item in self._items.items()
            if item.expires_at <= now or (item.used_at is not None and now - item.used_at > 60)
        ]
        for key in stale:
            self._items.pop(key, None)

    def create(self, safe_step: dict[str, Any]) -> tuple[Handoff, str]:
        if safe_step.get("type") != "cookies":
            raise HandoffError("Only cookie login steps may create a helper handoff")
        login_id = str(safe_step.get("login_id") or "")
        step_id = str(safe_step.get("step_id") or "")
        if not login_id or not step_id:
            raise HandoffError("The cookie login step is missing required process identifiers")

        token = secrets.token_urlsafe(32)
        handoff_id = secrets.token_urlsafe(18)
        now = time.time()
        item = Handoff(
            handoff_id=handoff_id,
            token_digest=self._digest(token),
            created_at=now,
            expires_at=now + self.ttl,
            login_id=login_id,
            step_id=step_id,
            txn_id=str(safe_step.get("txn_id") or ""),
            metadata={
                "type": "cookies",
                "instructions": str(safe_step.get("instructions") or ""),
                "cookies": dict(safe_step.get("cookies") or {}),
            },
        )
        with self._lock:
            self._purge_locked(now)
            self._items[handoff_id] = item
        return item, token

    def get(self, handoff_id: str, token: str, *, consume: bool = False) -> Handoff:
        if not handoff_id or not token:
            raise HandoffError("Missing helper pairing credentials")
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            item = self._items.get(handoff_id)
            if item is None:
                raise HandoffError("Helper pairing session not found or expired")
            if item.expires_at <= now:
                self._items.pop(handoff_id, None)
                raise HandoffError("Helper pairing session expired")
            if item.used_at is not None:
                raise HandoffError("Helper pairing session was already used")
            if not hmac.compare_digest(item.token_digest, self._digest(token)):
                raise HandoffError("Invalid helper pairing token")
            if consume:
                item.used_at = now
            return item

    def revoke(self, handoff_id: str) -> None:
        with self._lock:
            self._items.pop(handoff_id, None)


registry = HandoffRegistry()
