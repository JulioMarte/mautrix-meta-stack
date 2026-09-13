import base64
import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import patch


class FinalRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-final-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        global final, legacy
        final = importlib.import_module("final_app")
        legacy = final.legacy
        cls.client = final.application.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")

    def basic_header(self, password):
        encoded = base64.b64encode(("mautrix:" + password).encode()).decode()
        return {"Authorization": "Basic " + encoded}

    def configure_chatwoot_link(self, inbox_id="2", conversation_id=77, room_id="!portal:matrix.example.com"):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", str(inbox_id))
        legacy.set_setting("chatwoot_api_token", "token")
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?, ?, ?, ?, ?)",
                (room_id, 5, "source-5", conversation_id, int(time.time())),
            )
        return room_id

    def test_proxy_resolver_requires_internal_basic_auth(self):
        unauth = self.client.get("/internal/proxy")
        self.assertEqual(unauth.status_code, 404)
        wrong = self.client.get("/internal/proxy", headers=self.basic_header("wrong-secret-long-value"))
        self.assertEqual(wrong.status_code, 404)
        good = self.client.get("/internal/proxy", headers=self.basic_header("resolver-secret-long-value"))
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json, {"proxy_url": ""})

    def test_transitional_secret_path_is_disabled(self):
        response = self.client.get("/internal/proxy/resolver-secret-long-value")
        self.assertEqual(response.status_code, 404)

    def test_historical_event_before_activation_is_dropped(self):
        cutoff = int(time.time() * 1000)
        legacy.set_setting("chatwoot_enabled_at_ms", str(cutoff))
        old_event = {"origin_server_ts": cutoff - 60_000, "event_id": "$old"}
        with patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot("!portal:matrix.example.com", old_event)
        base.assert_not_called()

    def test_event_after_activation_is_forwarded_to_normal_filter_chain(self):
        cutoff = int(time.time() * 1000)
        legacy.set_setting("chatwoot_enabled_at_ms", str(cutoff))
        fresh_event = {"origin_server_ts": cutoff + 1, "event_id": "$fresh"}
        with patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot("!portal:matrix.example.com", fresh_event)
        base.assert_called_once_with("!portal:matrix.example.com", fresh_event)

    def test_activation_boundary_is_persisted_once(self):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        final.ensure_activation_boundary()
        first = legacy.get_setting("chatwoot_enabled_at_ms")
        self.assertTrue(first.isdigit())
        time.sleep(0.01)
        final.ensure_activation_boundary()
        self.assertEqual(legacy.get_setting("chatwoot_enabled_at_ms"), first)

    def test_conversation_inbox_parser_accepts_supported_shapes(self):
        self.assertEqual(final._conversation_inbox_id({"inbox_id": 2}), 2)
        self.assertEqual(final._conversation_inbox_id({"inbox": {"id": "3"}}), 3)
        self.assertEqual(final._conversation_inbox_id({"contact_inbox": {"inbox_id": 4}}), 4)
        self.assertEqual(final._conversation_inbox_id({"payload": {"conversation": {"inbox_id": "5"}}}), 5)
        self.assertIsNone(final._conversation_inbox_id({"status": "open"}))

    def test_chatwoot_reply_from_configured_inbox_is_delivered(self):
        room_id = self.configure_chatwoot_link(inbox_id="2")
        with patch.object(final.prod, "cw_get", return_value={"id": 77, "inbox_id": 2}) as lookup, \
             patch.object(final, "base_send_matrix_message", return_value={"event_id": "$sent"}) as send:
            result = final.guarded_send_matrix_message(room_id, "hello", "txn-1")
        lookup.assert_called_once_with("/api/v1/accounts/1/conversations/77")
        send.assert_called_once_with(room_id, "hello", "txn-1")
        self.assertEqual(result, {"event_id": "$sent"})

    def test_chatwoot_reply_from_other_inbox_is_ignored(self):
        room_id = self.configure_chatwoot_link(inbox_id="2")
        with patch.object(final.prod, "cw_get", return_value={"id": 77, "inbox_id": 99}), \
             patch.object(final, "base_send_matrix_message") as send:
            result = final.guarded_send_matrix_message(room_id, "must not pass", "txn-2")
        send.assert_not_called()
        self.assertTrue(result["ignored"])
        self.assertEqual(result["reason"], "outside_configured_chatwoot_inbox")

    def test_moved_conversation_is_blocked_even_if_room_was_already_linked(self):
        room_id = self.configure_chatwoot_link(inbox_id="6", conversation_id=123)
        with patch.object(final.prod, "cw_get", return_value={"id": 123, "contact_inbox": {"inbox_id": 7}}), \
             patch.object(final, "base_send_matrix_message") as send:
            result = final.guarded_send_matrix_message(room_id, "must not pass", "txn-3")
        send.assert_not_called()
        self.assertTrue(result["ignored"])

    def test_missing_inbox_id_fails_closed_without_matrix_delivery(self):
        room_id = self.configure_chatwoot_link(inbox_id="2")
        with patch.object(final.prod, "cw_get", return_value={"id": 77, "status": "open"}), \
             patch.object(final, "base_send_matrix_message") as send:
            with self.assertRaisesRegex(RuntimeError, "did not expose an inbox ID"):
                final.guarded_send_matrix_message(room_id, "must not pass", "txn-4")
        send.assert_not_called()

    def test_unmapped_room_fails_closed_without_matrix_delivery(self):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        with patch.object(final.prod, "cw_get") as lookup, \
             patch.object(final, "base_send_matrix_message") as send:
            with self.assertRaisesRegex(RuntimeError, "no linked Chatwoot conversation"):
                final.guarded_send_matrix_message("!unknown:matrix.example.com", "must not pass", "txn-5")
        lookup.assert_not_called()
        send.assert_not_called()

    def test_matrix_message_for_existing_link_in_configured_inbox_is_forwarded(self):
        room_id = self.configure_chatwoot_link(inbox_id="2", conversation_id=88)
        event = {"type": "m.room.message", "origin_server_ts": int(time.time() * 1000), "event_id": "$same"}
        with patch.object(final.prod, "cw_get", return_value={"id": 88, "inbox_id": 2}) as lookup, \
             patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot(room_id, event)
        lookup.assert_called_once_with("/api/v1/accounts/1/conversations/88")
        base.assert_called_once_with(room_id, event)

    def test_matrix_message_for_conversation_moved_to_other_inbox_is_blocked(self):
        room_id = self.configure_chatwoot_link(inbox_id="2", conversation_id=88)
        event = {"type": "m.room.message", "origin_server_ts": int(time.time() * 1000), "event_id": "$moved"}
        with patch.object(final.prod, "cw_get", return_value={"id": 88, "inbox_id": 9}), \
             patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot(room_id, event)
        base.assert_not_called()

    def test_new_unlinked_matrix_room_can_create_conversation_in_configured_inbox(self):
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        event = {"type": "m.room.message", "origin_server_ts": int(time.time() * 1000), "event_id": "$new"}
        with patch.object(final.prod, "cw_get") as lookup, \
             patch.object(final, "base_matrix_event_to_chatwoot") as base:
            final.guarded_matrix_event_to_chatwoot("!new:matrix.example.com", event)
        lookup.assert_not_called()
        base.assert_called_once_with("!new:matrix.example.com", event)


if __name__ == "__main__":
    unittest.main()
