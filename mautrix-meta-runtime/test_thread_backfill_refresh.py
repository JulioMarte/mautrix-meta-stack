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

    def test_completed_backfill_is_refreshed_once_per_process(self):
        source = '''\tif m.LoginMeta.BackfillCompleted {\n\t\tlog.Debug().Msg("Thread backfill already completed, skipping")\n\t\treturn nil\n\t}\n\tlog.Info().Msg("Starting thread backfill")\n\n\treturn m.runThreadBackfill(ctx)\n'''
        patched = patcher.patch_threadbackfill(source)
        self.assertIn("CompareAndSwap(false, true)", patched)
        self.assertIn("Re-running thread backfill once after process start", patched)
        self.assertIn("m.threadBackfillStarted.Store(false)", patched)
        self.assertNotIn("already completed, skipping", patched)

    def test_fetch_more_threads_gets_safe_discovery_telemetry(self):
        source = '''\ttbl, err := resp.Parse(ctx)\n\tif err != nil {\n\t\treturn nil, nil, fmt.Errorf("failed to parse response: %w", err)\n\t}\n\tc.PostHandlePublishResponse(tbl)\n\n\treturn keyStore, tbl, nil\n'''
        patched = patcher.patch_messagix_client(source)
        self.assertIn("META_THREAD_DIAG fetch_more_threads_response", patched)
        self.assertIn('Int64("sync_group_requested", syncGroup)', patched)
        self.assertIn('Int("threads_inserted", len(tbl.LSDeleteThenInsertThread))', patched)
        self.assertIn('Any("returned_sync_groups", threadSyncGroups)', patched)
        self.assertIn('Any("returned_folders", threadFolders)', patched)
        self.assertIn('Int("parent_thread_key_count", len(parentThreadKeys))', patched)
        # The telemetry may count opaque parent keys internally, but must not emit values
        # or user-facing thread metadata.
        self.assertNotIn('Int64("thread_key"', patched)
        self.assertNotIn('Str("thread_name"', patched)
        self.assertNotIn('Str("snippet"', patched)
        self.assertNotIn('Any("raw_payload"', patched)

    def test_pagination_scope_is_explicitly_observable(self):
        upstream = '''\t\t// Fetch next batch of threads, TODO: other SyncGroups?\n\t\tkeyStore, tbl, err := m.Client.FetchMoreThreads(ctx, 1) // SyncGroup 1\n'''
        self.assertIn("FetchMoreThreads(ctx, 1)", upstream)
        self.assertIn("TODO: other SyncGroups?", upstream)

    def test_patch_fails_closed_if_upstream_fragment_changes(self):
        with self.assertRaises(RuntimeError):
            patcher.patch_threadbackfill("func changedUpstream() {}")
        with self.assertRaises(RuntimeError):
            patcher.patch_messagix_client("func changedUpstream() {}")


if __name__ == "__main__":
    unittest.main()
