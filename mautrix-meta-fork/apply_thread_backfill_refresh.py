#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess
import sys

UPSTREAM_SHA = "ed37c9e6ce47e83dc75b9abea7b636302715b9bc"


def replace_once_text(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one upstream fragment, found {count}")
    return text.replace(old, new, 1)


def patch_client(text: str) -> str:
    return replace_once_text(
        text,
        "\tbackfillCollectors  map[int64]*BackfillCollector\n\tbackfillLock        sync.Mutex\n\tconnectLock         sync.Mutex\n",
        "\tbackfillCollectors    map[int64]*BackfillCollector\n\tbackfillLock          sync.Mutex\n\tthreadBackfillStarted atomic.Bool\n\tconnectLock           sync.Mutex\n",
        label="pkg/connector/client.go",
    )


def patch_threadbackfill(text: str) -> str:
    old = '''\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Debug().Msg("Thread backfill already completed, skipping")\n\t\treturn nil\n\t}\n\tlog.Info().Msg("Starting thread backfill")\n\n\treturn m.runThreadBackfill(ctx)\n'''
    new = '''\tif !m.threadBackfillStarted.CompareAndSwap(false, true) {\n\t\tlog.Debug().Msg("Thread backfill already started in this process, skipping duplicate trigger")\n\t\treturn nil\n\t}\n\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Info().Msg("Re-running thread backfill once after process start despite persisted completion marker")\n\t} else {\n\t\tlog.Info().Msg("Starting thread backfill")\n\t}\n\n\terr := m.runThreadBackfill(ctx)\n\tif err != nil {\n\t\tm.threadBackfillStarted.Store(false)\n\t}\n\treturn err\n'''
    return replace_once_text(text, old, new, label="pkg/connector/threadbackfill.go")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_thread_backfill_refresh.py <mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    if not (root / "go.mod").is_file():
        raise SystemExit(f"not a mautrix-meta source tree: {root}")

    actual_sha = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual_sha != UPSTREAM_SHA:
        raise SystemExit(f"unexpected mautrix-meta upstream SHA: {actual_sha}; expected {UPSTREAM_SHA}")

    client = root / "pkg/connector/client.go"
    threadbackfill = root / "pkg/connector/threadbackfill.go"
    try:
        client.write_text(patch_client(client.read_text()))
        threadbackfill.write_text(patch_threadbackfill(threadbackfill.read_text()))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
