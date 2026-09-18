import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class LegacyStub:
    MATRIX_ADMIN_MXID = "@admin:matrix.example.com"
    MATRIX_HOMESERVER = "http://synapse:8008"

    def __init__(self, path):
        self.path = path
        self.settings = {
            "chatwoot_account_id": "1",
            "chatwoot_inbox_id": "2",
            "chatwoot_base_url": "https://chatwoot.example.com",
            "matrix_next_batch": "s1",
        }

    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = str(value)

    def bridge_bot_mxid(self):
        return "@metabot:matrix.example.com"

    def chatwoot_url(self, path):
        return "https://chatwoot.example.com" + path

    def chatwoot_headers(self):
        return {"api_access_token": "test"}

    def matrix_headers(self):
        return {"Authorization": "Bearer test"}


class FakeResponse:
    def __init__(self, *, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = b"{}" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class ConversationLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.legacy = LegacyStub(str(Path(cls.tmp.name) / "integration.db"))
        with cls.legacy.db() as conn:
            conn.executescript(
                """
                CREATE TABLE room_links (
                  room_id TEXT PRIMARY KEY,
                  contact_id INTEGER NOT NULL,
                  source_id TEXT NOT NULL,
                  conversation_id INTEGER NOT NULL UNIQUE,
                  created_at INTEGER NOT NULL
                );
                """
            )

        cls.prod = types.SimpleNamespace(ensure_room_link=lambda room_id, sender: None)
        final_app = types.ModuleType("final_app")
        final_app.legacy = cls.legacy
        final_app.prod = cls.prod

        enhancements = types.ModuleType("runtime_enhancements")
        enhancements.handle_chatwoot_outgoing = Mock(return_value={"ok": True, "base": True})
        enhancements.repair_deleted_conversation = Mock(return_value=False)
        enhancements.auto_join_room = Mock(return_value=False)
        enhancements.import_recent_history = Mock(return_value=0)
        enhancements.enhanced_live_matrix_event = Mock()

        reconcile = types.ModuleType("meta_portal_reconcile")
        reconcile.verified_meta_portal = Mock(return_value=(True, "trusted"))

        cls.saved = {name: sys.modules.get(name) for name in (
            "final_app", "runtime_enhancements", "meta_portal_reconcile", "conversation_lifecycle_v10"
        )}
        sys.modules["final_app"] = final_app
        sys.modules["runtime_enhancements"] = enhancements
        sys.modules["meta_portal_reconcile"] = reconcile
        sys.modules.pop("conversation_lifecycle_v10", None)
        cls.enhancements = enhancements
        cls.module = importlib.import_module("conversation_lifecycle_v10")
        cls.module.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        for name, previous in cls.saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        cls.tmp.cleanup()

    def setUp(self):
        with self.legacy.db() as conn:
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM verified_meta_portals")
            conn.execute("DELETE FROM conversation_deletions")
        self.legacy.settings["matrix_next_batch"] = "s1"
        self.enhancements.handle_chatwoot_outgoing.reset_mock()
        self.enhancements.enhanced_live_matrix_event.reset_mock()

    def seed(self, room="!room:matrix.example.com", conversation=77, verified=True):
        with self.legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id,contact_id,source_id,conversation_id,created_at) VALUES(?,?,?,?,1)",
                (room, 3, "source", conversation),
            )
        if verified:
            self.module.remember_verified_portal(room)
        return room

    def trusted_leave(self):
        return {
            "timeline": {
                "events": [{
                    "type": "m.room.member",
                    "state_key": self.legacy.MATRIX_ADMIN_MXID,
                    "sender": self.legacy.bridge_bot_mxid(),
                    "content": {"membership": "leave"},
                }]
            }
        }

    def test_schema_contains_durable_retry_deadline(self):
        with self.legacy.db() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_deletions)")}
        self.assertIn("next_retry_at", columns)

    def test_chatwoot_delete_emits_native_event_and_retains_mapping_until_confirmation(self):
        room = self.seed()
        put = Mock(return_value=FakeResponse(payload={"event_id": "$delete"}))
        with patch.object(self.module.requests, "put", put):
            result = self.module.process_chatwoot_delete({
                "event": "conversation_deleted", "conversation_id": 77,
                "account": {"id": 1}, "inbox": {"id": 2},
            })
        self.assertTrue(result["remote_requested"])
        args, kwargs = put.call_args
        self.assertIn("/send/com.beeper.delete_chat/", args[0])
        self.assertEqual(kwargs["json"], {"delete_for_everyone": False, "from_message_request": False})
        self.assertIsNotNone(self.module._link_by_room(room))
        op = self.module._operation(77)
        self.assertEqual(op["origin"], "chatwoot")
        self.assertEqual(op["state"], "remote_requested")
        self.assertEqual(op["matrix_event_id"], "$delete")

    def test_observed_404_reuses_chatwoot_deletion_state_machine(self):
        room = self.seed()
        put = Mock(return_value=FakeResponse(payload={"event_id": "$delete-from-404"}))
        with patch.object(self.module.requests, "put", put):
            result = self.module.recover_missing_chatwoot_conversation(room, 77)

        self.assertTrue(result["remote_requested"])
        self.assertEqual(self.module._operation(77)["origin"], "chatwoot")
        self.assertEqual(self.module._operation(77)["state"], "remote_requested")
        self.assertIsNotNone(self.module._link_by_room(room))
        put.assert_called_once()

    def test_observed_404_does_not_act_on_changed_mapping(self):
        self.seed()
        with patch.object(self.module.requests, "put") as put:
            result = self.module.recover_missing_chatwoot_conversation(
                "!different:matrix.example.com", 77
            )
        put.assert_not_called()
        self.assertEqual(result["reason"], "stale_mapping_changed")

    def test_meta_confirmation_of_chatwoot_delete_does_not_delete_chatwoot_twice(self):
        room = self.seed()
        self.module._start_operation(77, room, "chatwoot", "remote_requested")
        with patch.object(self.module.requests, "delete") as delete:
            result = self.module.process_matrix_leave(room, self.trusted_leave())
        delete.assert_not_called()
        self.assertEqual(result["origin"], "chatwoot")
        self.assertIsNone(self.module._link_by_room(room))
        self.assertEqual(self.module._operation(77)["state"], "completed")

    def test_fast_bridge_confirmation_can_complete_chatwoot_pending_state(self):
        room = self.seed()
        self.module._start_operation(77, room, "chatwoot", "pending")
        with patch.object(self.module.requests, "delete") as delete:
            result = self.module.process_matrix_leave(room, self.trusted_leave())
        delete.assert_not_called()
        self.assertEqual(result["origin"], "chatwoot")
        self.assertEqual(self.module._operation(77)["state"], "completed")
        self.assertIsNone(self.module._link_by_room(room))

    def test_meta_delete_removes_chatwoot_and_callback_is_loop_suppressed(self):
        room = self.seed()
        delete = Mock(return_value=FakeResponse(status=204))
        with patch.object(self.module.requests, "delete", delete):
            result = self.module.process_matrix_leave(room, self.trusted_leave())
        self.assertEqual(result["origin"], "meta")
        self.assertIsNone(self.module._link_by_room(room))
        self.assertEqual(self.module._operation(77)["state"], "completed")
        callback = self.module.process_chatwoot_delete({
            "event": "conversation_deleted", "conversation_id": 77,
            "account": {"id": 1}, "inbox": {"id": 2},
        })
        self.assertEqual(callback["reason"], "meta_delete_loop_suppressed")

    def test_meta_delete_failure_is_persisted_and_retry_completes_after_sync_token_advances(self):
        room = self.seed()
        sync_payload = {
            "next_batch": "s2",
            "rooms": {"leave": {room: self.trusted_leave()}},
        }
        get = Mock(return_value=FakeResponse(payload=sync_payload))
        failing_delete = Mock(return_value=FakeResponse(status=503))

        with patch.object(self.module.requests, "get", get), patch.object(
            self.module.requests, "delete", failing_delete
        ):
            self.module.lifecycle_sync_once()

        self.assertEqual(self.legacy.get_setting("matrix_next_batch"), "s2")
        operation = self.module._operation(77)
        self.assertEqual(operation["origin"], "meta")
        self.assertEqual(operation["state"], "failed_retryable")
        self.assertEqual(operation["attempts"], 1)
        self.assertGreater(operation["next_retry_at"], 0)
        self.assertIsNotNone(self.module._link_by_room(room))

        success_delete = Mock(return_value=FakeResponse(status=204))
        with patch.object(self.module.requests, "delete", success_delete):
            result = self.module.reconcile_retryable_meta_deletions(
                now=int(operation["next_retry_at"])
            )

        self.assertEqual(result, {"processed": 1, "completed": 1, "failed": 0})
        self.assertIsNone(self.module._link_by_room(room))
        completed = self.module._operation(77)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["attempts"], 2)
        self.assertEqual(completed["next_retry_at"], 0)

    def test_remote_confirmed_survives_restart_before_chatwoot_http_attempt(self):
        room = self.seed()
        self.module._start_operation(77, room, "meta", "remote_confirmed")
        with patch.object(self.module.requests, "delete", Mock(return_value=FakeResponse(status=404))):
            result = self.module.reconcile_retryable_meta_deletions(now=0)
        self.assertEqual(result, {"processed": 1, "completed": 1, "failed": 0})
        self.assertEqual(self.module._operation(77)["state"], "completed")
        self.assertIsNone(self.module._link_by_room(room))

    def test_meta_retry_failure_uses_exponential_backoff_and_keeps_mapping(self):
        room = self.seed()
        self.module._start_operation(77, room, "meta", "remote_confirmed")
        self.module._update_operation(
            77,
            state="failed_retryable",
            error="first failure",
            increment_attempts=True,
            next_retry_at=100,
        )
        with patch.object(self.module.time, "time", return_value=100), patch.object(
            self.module.requests, "delete", Mock(return_value=FakeResponse(status=503))
        ):
            result = self.module.reconcile_retryable_meta_deletions(now=100)

        self.assertEqual(result, {"processed": 1, "completed": 0, "failed": 1})
        operation = self.module._operation(77)
        self.assertEqual(operation["state"], "failed_retryable")
        self.assertEqual(operation["attempts"], 2)
        self.assertEqual(operation["next_retry_at"], 160)
        self.assertIsNotNone(self.module._link_by_room(room))

    def test_retry_treats_chatwoot_404_as_idempotent_success(self):
        room = self.seed()
        self.module._start_operation(77, room, "meta", "remote_confirmed")
        self.module._update_operation(
            77,
            state="failed_retryable",
            error="timeout",
            increment_attempts=True,
            next_retry_at=1,
        )
        with patch.object(self.module.requests, "delete", Mock(return_value=FakeResponse(status=404))):
            result = self.module.reconcile_retryable_meta_deletions(now=1)
        self.assertEqual(result["completed"], 1)
        self.assertIsNone(self.module._link_by_room(room))
        self.assertEqual(self.module._operation(77)["state"], "completed")

    def test_invalid_state_transition_is_rejected(self):
        room = self.seed()
        self.module._start_operation(77, room, "chatwoot", "remote_requested")
        with self.assertRaisesRegex(RuntimeError, "invalid conversation deletion transition"):
            self.module._update_operation(77, state="failed_retryable")

    def test_untrusted_or_unverified_leave_never_deletes_chatwoot(self):
        room = self.seed(verified=False)
        with patch.object(self.module.requests, "delete") as delete:
            result = self.module.process_matrix_leave(room, self.trusted_leave())
        delete.assert_not_called()
        self.assertEqual(result["reason"], "portal_not_preverified")

        self.module.remember_verified_portal(room)
        bad = self.trusted_leave()
        bad["timeline"]["events"][0]["sender"] = self.legacy.MATRIX_ADMIN_MXID
        with patch.object(self.module.requests, "delete") as delete:
            result = self.module.process_matrix_leave(room, bad)
        delete.assert_not_called()
        self.assertEqual(result["reason"], "leave_not_from_bridge_bot")

    def test_wrong_chatwoot_scope_is_ignored(self):
        self.seed()
        with patch.object(self.module.requests, "put") as put:
            result = self.module.process_chatwoot_delete({
                "event": "conversation_deleted", "conversation_id": 77,
                "account": {"id": 1}, "inbox": {"id": 999},
            })
        put.assert_not_called()
        self.assertEqual(result["reason"], "outside_configured_chatwoot_inbox")

    def test_non_delete_callback_delegates_without_recursion(self):
        result = self.module.handle_chatwoot_event({"event": "message_created"})
        self.assertEqual(result, {"ok": True, "base": True})


if __name__ == "__main__":
    unittest.main()
