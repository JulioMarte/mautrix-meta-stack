import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).with_name("apply_thread_backfill_refresh.py")
spec = importlib.util.spec_from_file_location("runtime_thread_backfill_patch", SCRIPT)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class RuntimeThreadBackfillRefreshPatchTests(unittest.TestCase):
    def test_runtime_is_pinned_to_v26081_commit(self):
        self.assertEqual(patcher.UPSTREAM_SHA, "001f276beca5b90dead1bbc1351e1036e3f966a7")

    def test_client_gets_process_local_atomic_guard(self):
        source = (
            "type MetaClient struct {\n"
            "\tbackfillCollectors  map[int64]*BackfillCollector\n"
            "\tbackfillLock        sync.Mutex\n"
            "\tconnectLock         sync.Mutex\n"
            "}\n"
        )
        patched = patcher.patch_client(source)
        self.assertIn("threadBackfillStarted atomic.Bool", patched)

    def test_completed_backfill_is_refreshed_once_per_process_and_paginates_both_groups(self):
        source = '''func (m *MetaClient) StartThreadBackfill(ctx context.Context) error {
\tif m.Main.Config.ThreadBackfill.BatchCount == 0 {
\t\treturn nil
\t}

\tlog := m.UserLogin.Log.With().Str("action", "thread_backfill").Logger()
\tctx = log.WithContext(ctx)

\tif m.LoginMeta.BackfillCompleted {
\t\tlog.Debug().Msg("Thread backfill already completed, skipping")
\t\treturn nil
\t}
\tlog.Info().Msg("Starting thread backfill")

\treturn m.runThreadBackfill(ctx)
}

func (m *MetaClient) runThreadBackfill(ctx context.Context) error {
\tlog := zerolog.Ctx(ctx)
\tdelay := m.Main.Config.ThreadBackfill.BatchDelay
\tbatchLimit := m.Main.Config.ThreadBackfill.BatchCount
\tbatchCount := 0
\tvar prevMinThreadKey int64

\tfor {
\t\tif ctx.Err() != nil {
\t\t\treturn ctx.Err()
\t\t}

\t\t// Fetch next batch of threads, TODO: other SyncGroups?
\t\tkeyStore, tbl, err := m.Client.FetchMoreThreads(ctx, 1) // SyncGroup 1
\t\tif err != nil {
\t\t\tlog.Err(err).Msg("Failed to fetch more threads")
\t\t\treturn err
\t\t} else if tbl == nil {
\t\t\tlog.Info().Int("batches_processed", batchCount).Msg("Thread backfill complete - no more threads")
\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)
\t\t\treturn nil
\t\t}

\t\tbatchCount++

\t\t// Process received threads (handled via normal event flow)
\t\tm.parseAndQueueTable(ctx, tbl, false)

\t\t// Check if more threads available - note HasMoreBefore may never become false, so we watch
\t\t// for empty tables as well to identify when we've fully paginated.
\t\tif keyStore == nil || !keyStore.HasMoreBefore {
\t\t\tlog.Info().
\t\t\t\tInt("batches_processed", batchCount).
\t\t\t\tAny("keystore", keyStore).
\t\t\t\tMsg("Thread backfill complete - fully paginated (has no more)")
\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)
\t\t\treturn nil
\t\t} else if keyStore.MinThreadKey == prevMinThreadKey {
\t\t\tlog.Info().
\t\t\t\tInt("batches_processed", batchCount).
\t\t\t\tAny("keystore", keyStore).
\t\t\t\tMsg("Thread backfill complete - fully paginated (thread key did not change)")
\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)
\t\t\treturn nil
\t\t} else if batchLimit > 0 && batchCount >= batchLimit {
\t\t\tlog.Info().Int("batched_processed", batchCount).
\t\t\t\tMsg("Thread backfill complete - hit batch count limit")
\t\t\tm.markBackfillComplete(ctx, m.LoginMeta)
\t\t\treturn nil
\t\t}

\t\tprevMinThreadKey = keyStore.MinThreadKey

\t\tlog.Debug().
\t\t\tInt("batch", batchCount).
\t\t\tInt64("min_thread_key", keyStore.MinThreadKey).
\t\t\tInt64("min_activity_ts", keyStore.MinLastActivityTimestampMs).
\t\t\tMsg("Processed thread backfill batch")

\t\t// Rate limiting delay
\t\tif delay > 0 {
\t\t\tselect {
\t\t\tcase <-time.After(delay):
\t\t\tcase <-ctx.Done():
\t\t\t\treturn ctx.Err()
\t\t\t}
\t\t}
\t}
}
'''
        patched = patcher.patch_threadbackfill(source)
        self.assertIn("CompareAndSwap(false, true)", patched)
        self.assertIn("Re-running thread backfill once after process start", patched)
        self.assertIn("m.threadBackfillStarted.Store(false)", patched)
        self.assertIn("for _, syncGroup := range []int64{1, 95}", patched)
        self.assertIn("FetchMoreThreads(ctx, syncGroup)", patched)
        self.assertIn("all known sync groups paginated", patched)
        self.assertNotIn("TODO: other SyncGroups?", patched)
        self.assertNotIn("FetchMoreThreads(ctx, 1)", patched)

    def test_fetch_more_threads_gets_safe_discovery_telemetry(self):
        source = '''\ttbl, err := resp.Parse(ctx)\n\tif err != nil {\n\t\treturn nil, nil, fmt.Errorf("failed to parse response: %w", err)\n\t}\n\tc.PostHandlePublishResponse(tbl)\n\n\treturn keyStore, tbl, nil\n'''
        patched = patcher.patch_messagix_client(source)
        self.assertIn("META_THREAD_DIAG fetch_more_threads_response", patched)
        self.assertIn('Int64("sync_group_requested", syncGroup)', patched)
        self.assertIn('Int("threads_inserted", len(tbl.LSDeleteThenInsertThread))', patched)
        self.assertIn('Any("returned_sync_groups", threadSyncGroups)', patched)
        self.assertIn('Any("returned_folders", threadFolders)', patched)
        self.assertIn('Int("parent_thread_key_count", len(parentThreadKeys))', patched)
        self.assertNotIn('Int64("thread_key"', patched)
        self.assertNotIn('Str("thread_name"', patched)
        self.assertNotIn('Str("snippet"', patched)
        self.assertNotIn('Any("raw_payload"', patched)

    def test_message_backfill_failure_modes_are_observable_without_ids_or_bodies(self):
        source = '''\tif params.Forward && params.BundledData == nil {
\t\tzerolog.Ctx(ctx).Debug().Msg("Ignoring forward backfill without bundled data")
\t\treturn nil, nil
\t}
\t\t\tif !ok {
\t\t\t\tzerolog.Ctx(ctx).Warn().Msg("Can't backfill with non-FB message ID")
\t\t\t\treturn nil, nil
\t\t\t}
\t\t} else {
\t\t\tzerolog.Ctx(ctx).Warn().Msg("Can't backfill chat with no messages")
\t\t\treturn nil, nil
\t\t}
\t\t\t\t} else if time.Since(start) > timeout {
\t\t\t\t\tzerolog.Ctx(ctx).Error().Msg("Waiting for backfill collector timed out")
\treturn m.wrapBackfillEvents(ctx, params.Portal, upsert, params.AnchorMessage, params.Forward), nil
}
'''
        patched = patcher.patch_backfill(source)
        self.assertIn("META_HISTORY_DIAG forward_backfill_without_bundled_data", patched)
        self.assertIn("META_HISTORY_DIAG non_fb_anchor", patched)
        self.assertIn("META_HISTORY_DIAG no_anchor_or_bundled_messages", patched)
        self.assertIn("META_HISTORY_DIAG collector_timeout", patched)
        self.assertIn("META_HISTORY_DIAG fetch_messages_complete", patched)
        self.assertIn('Int("returned_messages", len(resp.Messages))', patched)
        self.assertNotIn('Str("message_id"', patched)
        self.assertNotIn('Str("body"', patched)
        self.assertNotIn('Any("resp_data"', patched)

    def test_patch_fails_closed_if_upstream_fragment_changes(self):
        with self.assertRaises(RuntimeError):
            patcher.patch_threadbackfill("func changedUpstream() {}")
        with self.assertRaises(RuntimeError):
            patcher.patch_messagix_client("func changedUpstream() {}")
        with self.assertRaises(RuntimeError):
            patcher.patch_backfill("func changedUpstream() {}")


if __name__ == "__main__":
    unittest.main()
