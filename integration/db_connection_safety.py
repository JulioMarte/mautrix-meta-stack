"""Make legacy SQLite access close connections after context-manager use.

sqlite3.Connection implements ``with`` as a transaction boundary only; it does
not close the connection on ``__exit__``. The integration code consistently uses
``with legacy.db() as conn`` and historically assumed that also released the
connection. This proxy preserves the existing ``db()`` API while making that
assumption true and preventing descriptor/connection leaks in long-running
runtime processes and tests.
"""
from __future__ import annotations

from typing import Any


class ClosingConnectionProxy:
    """Transparent connection proxy that closes after a ``with`` block."""

    def __init__(self, connection: Any):
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_connection":
            object.__setattr__(self, name, value)
        else:
            setattr(self._connection, name, value)

    def __enter__(self):
        self._connection.__enter__()
        return self._connection

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._connection.__exit__(exc_type, exc, tb)
        finally:
            self._connection.close()

    def close(self) -> None:
        self._connection.close()


def install(legacy_module: Any) -> None:
    """Wrap ``legacy_module.db`` once without changing callers."""
    if getattr(legacy_module, "_db_connection_safety_installed", False):
        return

    original_db = legacy_module.db

    def safe_db(*args, **kwargs):
        return ClosingConnectionProxy(original_db(*args, **kwargs))

    legacy_module.db = safe_db
    legacy_module._db_connection_safety_installed = True
