import importlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


class LegacyStub:
    def __init__(self):
        self.settings = {"history_import_days": "30"}

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = str(value)


class ImmediateThread:
    calls = []

    def __init__(self, *, target, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.__class__.calls.append((target, args, name, daemon))

    def start(self):
        return None


class HistorySafetyV9Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = LegacyStub()

        delivery = types.ModuleType("delivery_history_v2")
        delivery.DEFAULT_HISTORY_DAYS = 30
        delivery.MAX_HISTORY_DAYS = 3650

        enhancements = types.ModuleType("runtime_enhancements")
        enhancements.legacy = cls.legacy
        enhancements.operations_state = Mock(return_value={"history_limit": 30, "history_days": 30})

        def base_save(**kwargs):
            cls.legacy.set_setting("history_import_days", kwargs["history_limit"])
        enhancements.save_operations_settings = Mock(side_effect=base_save)

        reconcile = types.ModuleType("meta_portal_reconcile")
        reconcile.reconcile_meta_portals = Mock(return_value={"history_imported": 2, "linked": 4})

        cls.saved = {name: sys.modules.get(name) for name in (
            "delivery_history_v2", "runtime_enhancements", "meta_portal_reconcile", "history_safety_v9"
        )}
        sys.modules["delivery_history_v2"] = delivery
        sys.modules["runtime_enhancements"] = enhancements
        sys.modules["meta_portal_reconcile"] = reconcile
        sys.modules.pop("history_safety_v9", None)

        cls.delivery = delivery
        cls.enhancements = enhancements
        cls.reconcile = reconcile
        cls.module = importlib.import_module("history_safety_v9")

    @classmethod
    def tearDownClass(cls):
        for name, previous in cls.saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def setUp(self):
        self.legacy.settings = {"history_import_days": "30"}
        self.delivery.MAX_HISTORY_DAYS = 3650
        self.module._base_save_operations_settings.reset_mock()
        self.module._base_operations_state.reset_mock()
        self.module._base_operations_state.return_value = {"history_limit": 30, "history_days": 30}
        self.enhancements.save_operations_settings = self.module._base_save_operations_settings
        self.enhancements.operations_state = self.module._base_operations_state
        self.reconcile.reconcile_meta_portals.reset_mock()
        ImmediateThread.calls.clear()

    def test_install_clamps_previously_saved_extreme_window(self):
        self.legacy.set_setting("history_import_days", "1200")
        self.module.install()
        self.assertEqual(self.legacy.get_setting("history_import_days"), "30")
        self.assertEqual(self.delivery.MAX_HISTORY_DAYS, 30)

    def test_backend_rejects_more_than_thirty_days(self):
        self.module.install()
        with self.assertRaisesRegex(ValueError, "between 0 and 30 days"):
            self.module.save_operations_settings(
                auto_join=True, import_history=True, history_limit=31,
                sync_profiles=True, repair_deleted=True,
            )

    def test_expanding_window_requests_incremental_reconcile_without_deleting_state(self):
        self.legacy.set_setting("history_import_days", "5")
        self.module.install()
        with patch.object(self.module.threading, "Thread", ImmediateThread):
            self.module.save_operations_settings(
                auto_join=True, import_history=True, history_limit=30,
                sync_profiles=True, repair_deleted=True,
            )
        self.assertEqual(self.legacy.get_setting("history_import_days"), "30")
        self.assertEqual(len(ImmediateThread.calls), 1)
        _target, args, name, daemon = ImmediateThread.calls[0]
        self.assertEqual(args, (5, 30))
        self.assertEqual(name, "history-window-resync")
        self.assertTrue(daemon)

    def test_shrinking_window_does_not_trigger_resync(self):
        self.legacy.set_setting("history_import_days", "30")
        self.module.install()
        with patch.object(self.module.threading, "Thread", ImmediateThread):
            self.module.save_operations_settings(
                auto_join=True, import_history=True, history_limit=5,
                sync_profiles=True, repair_deleted=True,
            )
        self.assertEqual(ImmediateThread.calls, [])


if __name__ == "__main__":
    unittest.main()
