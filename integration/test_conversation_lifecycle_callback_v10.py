import importlib
import sys
import types
import unittest
from unittest.mock import Mock


class CallbackLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lifecycle = types.ModuleType("conversation_lifecycle_v10")
        lifecycle.process_chatwoot_delete = Mock(return_value={"ok": True, "deleted": True})

        delivery = types.ModuleType("delivery_history_v2")
        delivery.callback_outgoing_handler = Mock(return_value={"ok": True, "base": True})

        cls.saved = {name: sys.modules.get(name) for name in (
            "conversation_lifecycle_v10", "delivery_history_v2", "conversation_lifecycle_callback_v10"
        )}
        sys.modules["conversation_lifecycle_v10"] = lifecycle
        sys.modules["delivery_history_v2"] = delivery
        sys.modules.pop("conversation_lifecycle_callback_v10", None)
        cls.lifecycle = lifecycle
        cls.delivery = delivery
        cls.module = importlib.import_module("conversation_lifecycle_callback_v10")

    @classmethod
    def tearDownClass(cls):
        for name, previous in cls.saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

    def setUp(self):
        self.lifecycle.process_chatwoot_delete.reset_mock()
        self.delivery.callback_outgoing_handler.reset_mock()

    def test_delete_requires_verified_hmac(self):
        with self.assertRaisesRegex(RuntimeError, "requires a verified"):
            self.module.callback_handler(
                {"event": "conversation_deleted", "conversation_id": 7},
                signature_verified=False,
            )
        self.lifecycle.process_chatwoot_delete.assert_not_called()

    def test_signed_delete_reaches_lifecycle(self):
        result = self.module.callback_handler(
            {"event": "conversation_deleted", "conversation_id": 7},
            signature_verified=True,
        )
        self.assertEqual(result, {"ok": True, "deleted": True})
        self.lifecycle.process_chatwoot_delete.assert_called_once()

    def test_non_delete_preserves_existing_production_dispatcher(self):
        result = self.module.callback_handler(
            {"event": "message_created"}, signature_verified=False
        )
        self.assertEqual(result, {"ok": True, "base": True})
        self.module._base_callback_handler.assert_called_once_with(
            {"event": "message_created"}, signature_verified=False
        )


if __name__ == "__main__":
    unittest.main()
