import importlib.util
import pathlib
import unittest


SCRIPT = pathlib.Path(__file__).with_name("apply_thread_backfill_refresh.py")
spec = importlib.util.spec_from_file_location("thread_backfill_patch", SCRIPT)
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)


class ThreadBackfillRefreshPatchTests(unittest.TestCase):
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

    def test_patch_fails_closed_if_upstream_fragment_changes(self):
        with self.assertRaises(RuntimeError):
            patcher.patch_threadbackfill("func changedUpstream() {}")


if __name__ == "__main__":
    unittest.main()
