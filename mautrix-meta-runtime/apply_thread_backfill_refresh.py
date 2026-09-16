#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import subprocess
import sys

UPSTREAM_SHA = "001f276beca5b90dead1bbc1351e1036e3f966a7"  # v0.2608.1


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one upstream fragment, found {count}")
    return text.replace(old, new, 1)


def patch_client(text: str) -> str:
    return replace_once(
        text,
        "\tbackfillCollectors  map[int64]*BackfillCollector\n\tbackfillLock        sync.Mutex\n\tconnectLock         sync.Mutex\n",
        "\tbackfillCollectors    map[int64]*BackfillCollector\n\tbackfillLock          sync.Mutex\n\tthreadBackfillStarted atomic.Bool\n\tconnectLock           sync.Mutex\n",
        label="pkg/connector/client.go",
    )


def patch_threadbackfill(text: str) -> str:
    old = '''\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Debug().Msg("Thread backfill already completed, skipping")\n\t\treturn nil\n\t}\n\tlog.Info().Msg("Starting thread backfill")\n\n\treturn m.runThreadBackfill(ctx)\n'''
    new = '''\tif !m.threadBackfillStarted.CompareAndSwap(false, true) {\n\t\tlog.Debug().Msg("Thread backfill already started in this process, skipping duplicate trigger")\n\t\treturn nil\n\t}\n\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Info().Msg("Re-running thread backfill once after process start despite persisted completion marker")\n\t} else {\n\t\tlog.Info().Msg("Starting thread backfill")\n\t}\n\n\terr := m.runThreadBackfill(ctx)\n\tif err != nil {\n\t\tm.threadBackfillStarted.Store(false)\n\t}\n\treturn err\n'''
    return replace_once(text, old, new, label="pkg/connector/threadbackfill.go")


def patch_messagix_client(text: str) -> str:
    old = '''\ttbl, err := resp.Parse(ctx)\n\tif err != nil {\n\t\treturn nil, nil, fmt.Errorf("failed to parse response: %w", err)\n\t}\n\tc.PostHandlePublishResponse(tbl)\n\n\treturn keyStore, tbl, nil\n'''
    new = '''\ttbl, err := resp.Parse(ctx)\n\tif err != nil {\n\t\treturn nil, nil, fmt.Errorf("failed to parse response: %w", err)\n\t}\n\n\t// Safe discovery telemetry: counts and Meta routing buckets only. Never log\n\t// thread IDs, names, message snippets, cookies, tokens, or raw payloads here.\n\tthreadSyncGroups := make(map[int64]int)\n\tthreadFolders := make(map[string]int)\n\tparentThreadKeys := make(map[int64]struct{})\n\tfor _, thread := range tbl.LSDeleteThenInsertThread {\n\t\tthreadSyncGroups[thread.SyncGroup]++\n\t\tthreadFolders[thread.FolderName]++\n\t\tparentThreadKeys[thread.ParentThreadKey] = struct{}{}\n\t}\n\tzerolog.Ctx(ctx).Info().\n\t\tInt64("sync_group_requested", syncGroup).\n\t\tInt("threads_inserted", len(tbl.LSDeleteThenInsertThread)).\n\t\tInt("threads_updated", len(tbl.LSUpdateOrInsertThread)).\n\t\tInt("threads_verified", len(tbl.LSVerifyThreadExists)).\n\t\tInt("sync_group_range_updates", len(tbl.LSUpsertSyncGroupThreadsRange)).\n\t\tInt("thread_range_v2_updates", len(tbl.LSUpdateThreadsRangesV2)).\n\t\tInt("folder_updates", len(tbl.LSUpsertFolder)).\n\t\tInt("parent_thread_key_count", len(parentThreadKeys)).\n\t\tAny("returned_sync_groups", threadSyncGroups).\n\t\tAny("returned_folders", threadFolders).\n\t\tMsg("META_THREAD_DIAG fetch_more_threads_response")\n\n\tc.PostHandlePublishResponse(tbl)\n\n\treturn keyStore, tbl, nil\n'''
    return replace_once(text, old, new, label="pkg/messagix/client.go")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_thread_backfill_refresh.py <mautrix-meta-source-dir>")
    root = pathlib.Path(sys.argv[1]).resolve()
    actual_sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual_sha != UPSTREAM_SHA:
        raise SystemExit(f"unexpected mautrix-meta upstream SHA: {actual_sha}; expected {UPSTREAM_SHA}")

    connector_client = root / "pkg/connector/client.go"
    threadbackfill = root / "pkg/connector/threadbackfill.go"
    messagix_client = root / "pkg/messagix/client.go"
    try:
        connector_client.write_text(patch_client(connector_client.read_text()))
        threadbackfill.write_text(patch_threadbackfill(threadbackfill.read_text()))
        messagix_client.write_text(patch_messagix_client(messagix_client.read_text()))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
