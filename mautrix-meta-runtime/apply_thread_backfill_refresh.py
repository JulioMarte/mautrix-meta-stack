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
    start_old = '''\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Debug().Msg("Thread backfill already completed, skipping")\n\t\treturn nil\n\t}\n\tlog.Info().Msg("Starting thread backfill")\n\n\treturn m.runThreadBackfill(ctx)\n'''
    start_new = '''\tif !m.threadBackfillStarted.CompareAndSwap(false, true) {\n\t\tlog.Debug().Msg("Thread backfill already started in this process, skipping duplicate trigger")\n\t\treturn nil\n\t}\n\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Info().Msg("Re-running thread backfill once after process start despite persisted completion marker")\n\t} else {\n\t\tlog.Info().Msg("Starting thread backfill")\n\t}\n\n\terr := m.runThreadBackfill(ctx)\n\tif err != nil {\n\t\tm.threadBackfillStarted.Store(false)\n\t}\n\treturn err\n'''
    text = replace_once(text, start_old, start_new, label="pkg/connector/threadbackfill.go start")

    run_old = '''func (m *MetaClient) runThreadBackfill(ctx context.Context) error {\n\tlog := zerolog.Ctx(ctx)\n\tdelay := m.Main.Config.ThreadBackfill.BatchDelay\n\tbatchLimit := m.Main.Config.ThreadBackfill.BatchCount\n\tbatchCount := 0\n\tvar prevMinThreadKey int64\n\n\tfor {\n\t\tif ctx.Err() != nil {\n\t\t\treturn ctx.Err()\n\t\t}\n\n\t\t// Fetch next batch of threads, TODO: other SyncGroups?\n\t\tkeyStore, tbl, err := m.Client.FetchMoreThreads(ctx, 1) // SyncGroup 1\n\t\tif err != nil {\n\t\t\tlog.Err(err).Msg("Failed to fetch more threads")\n\t\t\treturn err\n\t\t} else if tbl == nil {\n\t\t\tlog.Info().Int("batches_processed", batchCount).Msg("Thread backfill complete - no more threads")\n\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)\n\t\t\treturn nil\n\t\t}\n\n\t\tbatchCount++\n\n\t\t// Process received threads (handled via normal event flow)\n\t\tm.parseAndQueueTable(ctx, tbl, false)\n\n\t\t// Check if more threads available - note HasMoreBefore may never become false, so we watch\n\t\t// for empty tables as well to identify when we've fully paginated.\n\t\tif keyStore == nil || !keyStore.HasMoreBefore {\n\t\t\tlog.Info().\n\t\t\t\tInt("batches_processed", batchCount).\n\t\t\t\tAny("keystore", keyStore).\n\t\t\t\tMsg("Thread backfill complete - fully paginated (has no more)")\n\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)\n\t\t\treturn nil\n\t\t} else if keyStore.MinThreadKey == prevMinThreadKey {\n\t\t\tlog.Info().\n\t\t\t\tInt("batches_processed", batchCount).\n\t\t\t\tAny("keystore", keyStore).\n\t\t\t\tMsg("Thread backfill complete - fully paginated (thread key did not change)")\n\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)\n\t\t\treturn nil\n\t\t} else if batchLimit > 0 && batchCount >= batchLimit {\n\t\t\tlog.Info().Int("batched_processed", batchCount).\n\t\t\t\tMsg("Thread backfill complete - hit batch count limit")\n\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)\n\t\t\treturn nil\n\t\t}\n\n\t\tprevMinThreadKey = keyStore.MinThreadKey\n\n\t\tlog.Debug().\n\t\t\tInt("batch", batchCount).\n\t\t\tInt64("min_thread_key", keyStore.MinThreadKey).\n\t\t\tInt64("min_activity_ts", keyStore.MinLastActivityTimestampMs).\n\t\t\tMsg("Processed thread backfill batch")\n\n\t\t// Rate limiting delay\n\t\tif delay > 0 {\n\t\t\tselect {\n\t\t\tcase <-time.After(delay):\n\t\t\tcase <-ctx.Done():\n\t\t\t\treturn ctx.Err()\n\t\t\t}\n\t\t}\n\t}\n}\n'''
    run_new = '''func (m *MetaClient) runThreadBackfill(ctx context.Context) error {\n\tlog := zerolog.Ctx(ctx)\n\tdelay := m.Main.Config.ThreadBackfill.BatchDelay\n\tbatchLimit := m.Main.Config.ThreadBackfill.BatchCount\n\ttotalBatchCount := 0\n\n\t// Meta's initial fetch explicitly covers both sync groups 1 and 95. Upstream\n\t// v0.2608.1 only paginates group 1 afterwards, which can strand older threads\n\t// that belong to group 95. Keep the same global batch limit semantics while\n\t// exhausting both known thread key stores before persisting completion.\n\tfor _, syncGroup := range []int64{1, 95} {\n\t\tgroupBatchCount := 0\n\t\tvar prevMinThreadKey int64\n\t\tlog.Info().Int64("sync_group", syncGroup).Msg("Starting thread backfill sync group")\n\n\t\tfor {\n\t\t\tif ctx.Err() != nil {\n\t\t\t\treturn ctx.Err()\n\t\t\t}\n\n\t\t\tkeyStore, tbl, err := m.Client.FetchMoreThreads(ctx, syncGroup)\n\t\t\tif err != nil {\n\t\t\t\tlog.Err(err).Int64("sync_group", syncGroup).Msg("Failed to fetch more threads")\n\t\t\t\treturn err\n\t\t\t} else if tbl == nil {\n\t\t\t\tlog.Info().\n\t\t\t\t\tInt64("sync_group", syncGroup).\n\t\t\t\t\tInt("group_batches_processed", groupBatchCount).\n\t\t\t\t\tInt("total_batches_processed", totalBatchCount).\n\t\t\t\t\tMsg("Thread backfill sync group complete - no more threads")\n\t\t\t\tbreak\n\t\t\t}\n\n\t\t\tgroupBatchCount++\n\t\t\ttotalBatchCount++\n\n\t\t\t// Process received threads through the normal event path.\n\t\t\tm.parseAndQueueTable(ctx, tbl, false)\n\n\t\t\tif keyStore == nil || !keyStore.HasMoreBefore {\n\t\t\t\tlog.Info().\n\t\t\t\t\tInt64("sync_group", syncGroup).\n\t\t\t\t\tInt("group_batches_processed", groupBatchCount).\n\t\t\t\t\tInt("total_batches_processed", totalBatchCount).\n\t\t\t\t\tMsg("Thread backfill sync group complete - fully paginated")\n\t\t\t\tbreak\n\t\t\t} else if keyStore.MinThreadKey == prevMinThreadKey {\n\t\t\t\tlog.Info().\n\t\t\t\t\tInt64("sync_group", syncGroup).\n\t\t\t\t\tInt("group_batches_processed", groupBatchCount).\n\t\t\t\t\tInt("total_batches_processed", totalBatchCount).\n\t\t\t\t\tMsg("Thread backfill sync group complete - thread key did not change")\n\t\t\t\tbreak\n\t\t\t} else if batchLimit > 0 && totalBatchCount >= batchLimit {\n\t\t\t\tlog.Info().\n\t\t\t\t\tInt64("sync_group", syncGroup).\n\t\t\t\t\tInt("total_batches_processed", totalBatchCount).\n\t\t\t\t\tMsg("Thread backfill complete - hit global batch count limit")\n\t\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)\n\t\t\t\treturn nil\n\t\t\t}\n\n\t\t\tprevMinThreadKey = keyStore.MinThreadKey\n\n\t\t\tlog.Debug().\n\t\t\t\tInt64("sync_group", syncGroup).\n\t\t\t\tInt("group_batch", groupBatchCount).\n\t\t\t\tInt("total_batch", totalBatchCount).\n\t\t\t\tMsg("Processed thread backfill batch")\n\n\t\t\tif delay > 0 {\n\t\t\t\tselect {\n\t\t\t\tcase <-time.After(delay):\n\t\t\t\tcase <-ctx.Done():\n\t\t\t\t\treturn ctx.Err()\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n\n\tlog.Info().Int("batches_processed", totalBatchCount).Msg("Thread backfill complete - all known sync groups paginated")\n\tm.markBackfillComplete(ctx, m.LoginMeta)\n\treturn nil\n}\n'''
    return replace_once(text, run_old, run_new, label="pkg/connector/threadbackfill.go pagination")


def patch_messagix_client(text: str) -> str:
    old = '''\ttbl, err := resp.Parse(ctx)\n\tif err != nil {\n\t\treturn nil, nil, fmt.Errorf("failed to parse response: %w", err)\n\t}\n\tc.PostHandlePublishResponse(tbl)\n\n\treturn keyStore, tbl, nil\n'''
    new = '''\ttbl, err := resp.Parse(ctx)\n\tif err != nil {\n\t\treturn nil, nil, fmt.Errorf("failed to parse response: %w", err)\n\t}\n\n\t// Safe discovery telemetry: counts and Meta routing buckets only. Never log\n\t// thread IDs, names, message snippets, cookies, tokens, or raw payloads here.\n\tthreadSyncGroups := make(map[int64]int)\n\tthreadFolders := make(map[string]int)\n\tparentThreadKeys := make(map[int64]struct{})\n\tfor _, thread := range tbl.LSDeleteThenInsertThread {\n\t\tthreadSyncGroups[thread.SyncGroup]++\n\t\tthreadFolders[thread.FolderName]++\n\t\tparentThreadKeys[thread.ParentThreadKey] = struct{}{}\n\t}\n\tzerolog.Ctx(ctx).Info().\n\t\tInt64("sync_group_requested", syncGroup).\n\t\tInt("threads_inserted", len(tbl.LSDeleteThenInsertThread)).\n\t\tInt("threads_updated", len(tbl.LSUpdateOrInsertThread)).\n\t\tInt("threads_verified", len(tbl.LSVerifyThreadExists)).\n\t\tInt("sync_group_range_updates", len(tbl.LSUpsertSyncGroupThreadsRange)).\n\t\tInt("thread_range_v2_updates", len(tbl.LSUpdateThreadsRangesV2)).\n\t\tInt("folder_updates", len(tbl.LSUpsertFolder)).\n\t\tInt("parent_thread_key_count", len(parentThreadKeys)).\n\t\tAny("returned_sync_groups", threadSyncGroups).\n\t\tAny("returned_folders", threadFolders).\n\t\tMsg("META_THREAD_DIAG fetch_more_threads_response")\n\n\tc.PostHandlePublishResponse(tbl)\n\n\treturn keyStore, tbl, nil\n'''
    return replace_once(text, old, new, label="pkg/messagix/client.go")


def patch_backfill(text: str) -> str:
    text = replace_once(
        text,
        '''\tif params.Forward && params.BundledData == nil {\n\t\tzerolog.Ctx(ctx).Debug().Msg("Ignoring forward backfill without bundled data")\n\t\treturn nil, nil\n\t}\n''',
        '''\tif params.Forward && params.BundledData == nil {\n\t\tzerolog.Ctx(ctx).Info().\n\t\t\tInt("requested_count", params.Count).\n\t\t\tBool("has_anchor", params.AnchorMessage != nil).\n\t\t\tMsg("META_HISTORY_DIAG forward_backfill_without_bundled_data")\n\t\tzerolog.Ctx(ctx).Debug().Msg("Ignoring forward backfill without bundled data")\n\t\treturn nil, nil\n\t}\n''',
        label="pkg/connector/backfill.go forward without bundle",
    )
    text = replace_once(
        text,
        '''\t\t\tif !ok {\n\t\t\t\tzerolog.Ctx(ctx).Warn().Msg("Can't backfill with non-FB message ID")\n\t\t\t\treturn nil, nil\n\t\t\t}\n''',
        '''\t\t\tif !ok {\n\t\t\t\tzerolog.Ctx(ctx).Warn().\n\t\t\t\t\tBool("forward", params.Forward).\n\t\t\t\t\tInt("requested_count", params.Count).\n\t\t\t\t\tMsg("META_HISTORY_DIAG non_fb_anchor")\n\t\t\t\tzerolog.Ctx(ctx).Warn().Msg("Can't backfill with non-FB message ID")\n\t\t\t\treturn nil, nil\n\t\t\t}\n''',
        label="pkg/connector/backfill.go non-FB anchor",
    )
    text = replace_once(
        text,
        '''\t\t} else {\n\t\t\tzerolog.Ctx(ctx).Warn().Msg("Can't backfill chat with no messages")\n\t\t\treturn nil, nil\n\t\t}\n''',
        '''\t\t} else {\n\t\t\tzerolog.Ctx(ctx).Warn().\n\t\t\t\tBool("forward", params.Forward).\n\t\t\t\tInt("requested_count", params.Count).\n\t\t\t\tMsg("META_HISTORY_DIAG no_anchor_or_bundled_messages")\n\t\t\tzerolog.Ctx(ctx).Warn().Msg("Can't backfill chat with no messages")\n\t\t\treturn nil, nil\n\t\t}\n''',
        label="pkg/connector/backfill.go no anchor",
    )
    text = replace_once(
        text,
        '''\t\t\t\t} else if time.Since(start) > timeout {\n\t\t\t\t\tzerolog.Ctx(ctx).Error().Msg("Waiting for backfill collector timed out")\n''',
        '''\t\t\t\t} else if time.Since(start) > timeout {\n\t\t\t\t\tzerolog.Ctx(ctx).Error().\n\t\t\t\t\t\tBool("forward", params.Forward).\n\t\t\t\t\t\tInt("requested_count", params.Count).\n\t\t\t\t\t\tInt("collected_messages", len(collector.Messages)).\n\t\t\t\t\t\tMsg("META_HISTORY_DIAG collector_timeout")\n\t\t\t\t\tzerolog.Ctx(ctx).Error().Msg("Waiting for backfill collector timed out")\n''',
        label="pkg/connector/backfill.go timeout",
    )
    text = replace_once(
        text,
        '''\treturn m.wrapBackfillEvents(ctx, params.Portal, upsert, params.AnchorMessage, params.Forward), nil\n}\n''',
        '''\tresp := m.wrapBackfillEvents(ctx, params.Portal, upsert, params.AnchorMessage, params.Forward)\n\tzerolog.Ctx(ctx).Info().\n\t\tBool("forward", params.Forward).\n\t\tInt("requested_count", params.Count).\n\t\tInt("returned_messages", len(resp.Messages)).\n\t\tBool("has_more", resp.HasMore).\n\t\tMsg("META_HISTORY_DIAG fetch_messages_complete")\n\treturn resp, nil\n}\n''',
        label="pkg/connector/backfill.go completion",
    )
    return text


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
    backfill = root / "pkg/connector/backfill.go"
    try:
        connector_client.write_text(patch_client(connector_client.read_text()))
        threadbackfill.write_text(patch_threadbackfill(threadbackfill.read_text()))
        messagix_client.write_text(patch_messagix_client(messagix_client.read_text()))
        backfill.write_text(patch_backfill(backfill.read_text()))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
