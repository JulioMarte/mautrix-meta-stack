#!/usr/bin/env python3
"""Make mautrix-meta refresh older thread discovery on every full connection.

Upstream persists UserLoginMetadata.BackfillCompleted forever after the first full
pagination. That is efficient for a generic bridge, but it means a managed account
can reconnect/re-authenticate and never ask Facebook for older threads again. For
this single-account product we prefer correctness: a full connection re-runs the
paginated thread discovery. FetchMoreThreads still stops at the remote pagination
boundary and the configured inter-page delay remains intact.
"""
from __future__ import annotations

import pathlib
import sys

root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
path = root / "pkg/connector/threadbackfill.go"
text = path.read_text(encoding="utf-8")
old = '''\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Debug().Msg("Thread backfill already completed, skipping")\n\t\treturn nil\n\t}\n\tlog.Info().Msg("Starting thread backfill")\n'''
new = '''\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Info().Msg("Thread backfill completed previously; refreshing thread discovery")\n\t} else {\n\t\tlog.Info().Msg("Starting thread backfill")\n\t}\n'''
if old not in text:
    raise SystemExit("expected upstream thread backfill completion guard not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
