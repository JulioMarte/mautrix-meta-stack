import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

import yaml


class DeliveryHistoryV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-delivery-history-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            yaml.safe_dump({
                "id": "meta",
                "sender_localpart": "metabot",
                "namespaces": {
                    "users": [{"regex": r"^@meta_[0-9]+:matrix\.example\.com$", "exclusive": True}],
                    "aliases": [],
                    "rooms": [],
                },
            }, fh, sort_keys=False)
        global module, runtime, legacy, prod, enhancements
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("delivery_history_v2")
        enhancements = importlib.import_module("runtime_enhancements")
        legacy = runtime.legacy
        prod = runtime.prod
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        legacy.set_setting("import_history_on_join", "1")
        legacy.set_setting("history_import_days", "30")
        legacy.set_setting("sync_contact_profiles", "1")
        legacy.set_setting("repair_deleted_conversations", "1")

    def insert_link(self, room="!portal:matrix.example.com", conversation=77, contact=5):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?, ?, ?, ?, ?)",
                (room, contact, "source-5", conversation, int(time.time())),
            )
        return room

    def test_contact_parser_accepts_dict_and_list_payload_shapes(self):
        self.assertEqual(module.robust_contact_object({"payload": {"id": 5}})["id"], 5)
        self.assertEqual(module.robust_contact_object({"payload": [{"id": 6}]})["id"], 6)
        self.assertEqual(module.robust_contact_object({"payload": {"contact": {"id": 7}}})["id"], 7)

    def test_history_setting_is_days_and_saves_days(self):
        legacy.set_setting("history_import_days", "14")
        self.assertEqual(module.operations_state_days()["history_days"], 14)
        module.save_operations_settings_days(
            auto_join=True, import_history=True, history_limit=45,
            sync_profiles=True, repair_deleted=True,
        )
        self.assertEqual(legacy.get_setting("history_import_days"), "45")

    def test_history_paginates_until_day_cutoff_and_imports_oldest_first(self):
        now = 2_000_000_000
        day = 86400
        legacy.set_setting("history_import_days", "2")
        newer = {"event_id": "$new", "type": "m.room.message", "sender": "@meta_2:matrix.example.com",
                 "origin_server_ts": int((now - day // 2) * 1000), "content": {"msgtype": "m.text", "body": "new"}}
        middle = {"event_id": "$middle", "type": "m.room.message", "sender": "@meta_2:matrix.example.com",
                  "origin_server_ts": int((now - day) * 1000), "content": {"msgtype": "m.text", "body": "middle"}}
        old = {"event_id": "$old", "type": "m.room.message", "sender": "@meta_2:matrix.example.com",
               "origin_server_ts": int((now - 3 * day) * 1000), "content": {"msgtype": "m.text", "body": "old"}}
        page1 = Mock(); page1.json.return_value = {"chunk": [newer], "end": "t2"}
        page2 = Mock(); page2.json.return_value = {"chunk": [middle, old], "end": "t3"}
        seen = set()
        ordered = []

        def fake_seen(event_id):
            return event_id in seen

        def fake_import(room_id, event):
            ordered.append(event["event_id"])
            seen.add(event["event_id"])

        with patch.object(module.time, "time", return_value=now), \
             patch.object(enhancements, "_matrix_get", side_effect=[page1, page2]) as matrix_get, \
             patch.object(module.autojoin_verify, "trusted_meta_inviter", return_value=(True, "ghost")), \
             patch.object(legacy, "event_seen", side_effect=fake_seen), \
             patch.object(prod, "matrix_event_to_chatwoot", side_effect=fake_import):
            imported = module.import_recent_history_days("!room:matrix.example.com")

        self.assertEqual(imported, 2)
        self.assertEqual(ordered, ["$middle", "$new"])
        self.assertEqual(matrix_get.call_count, 2)
        self.assertEqual(matrix_get.call_args_list[1].kwargs["params"]["from"], "t2")

    def test_historical_admin_message_is_imported_as_outgoing_without_replay(self):
        now = 2_000_000_000
        customer = {"event_id": "$customer", "type": "m.room.message", "sender": "@meta_2:matrix.example.com",
                    "origin_server_ts": int((now - 20) * 1000), "content": {"msgtype": "m.text", "body": "hello"}}
        agent = {"event_id": "$agent", "type": "m.room.message", "sender": "@admin:matrix.example.com",
                 "origin_server_ts": int((now - 10) * 1000), "content": {"msgtype": "m.text", "body": "reply"}}
        response = Mock(); response.json.return_value = {"chunk": [agent, customer]}
        link = {"conversation_id": 77}
        seen = set()

        def fake_seen(event_id):
            return event_id in seen

        def fake_incoming(room_id, event):
            seen.add(event["event_id"])

        def fake_mark(event_id, direction):
            seen.add(event_id)

        with patch.object(module.time, "time", return_value=now), \
             patch.object(enhancements, "_matrix_get", return_value=response), \
             patch.object(module.autojoin_verify, "trusted_meta_inviter", return_value=(True, "ghost")), \
             patch.object(legacy, "event_seen", side_effect=fake_seen), \
             patch.object(legacy, "mark_event", side_effect=fake_mark), \
             patch.object(prod, "matrix_event_to_chatwoot", side_effect=fake_incoming), \
             patch.object(prod, "ensure_room_link", return_value=link), \
             patch.object(legacy, "cw_post", return_value={}) as cw_post:
            imported = module.import_recent_history_days("!room:matrix.example.com")

        self.assertEqual(imported, 2)
        payload = cw_post.call_args.args[1]
        self.assertEqual(payload["message_type"], "outgoing")
        self.assertTrue(payload["content_attributes"][module.HISTORY_MARKER])
        self.assertEqual(payload["content_attributes"]["matrix_event_id"], "$agent")

        callback = {
            "event": "message_created", "id": 901, "message_type": "outgoing", "private": False,
            "content": "reply", "content_attributes": {module.HISTORY_MARKER: True},
            "conversation": {"id": 77, "inbox_id": 2},
        }
        with patch.object(legacy, "send_matrix_message") as send:
            result = module.handle_chatwoot_outgoing_verified(callback, signature_verified=True)
        self.assertEqual(result.get("reason"), "history_import")
        send.assert_not_called()

    def test_outgoing_requires_positive_matrix_event_before_marking_processed(self):
        room = self.insert_link(conversation=123)
        payload = {
            "event": "message_created", "id": 999, "message_type": "outgoing", "private": False,
            "content": "hello", "conversation": {"id": 123, "inbox_id": 2},
        }
        with patch.object(legacy, "send_matrix_message", return_value={"event_id": "$mx"}) as send, \
             patch.object(module, "verify_matrix_event") as verify:
            result = module.handle_chatwoot_outgoing_verified(payload, signature_verified=True)
        self.assertEqual(result["matrix_event_id"], "$mx")
        send.assert_called_once_with(room, "hello", "cw-999")
        verify.assert_called_once_with(room, "$mx", "hello")
        self.assertTrue(legacy.event_seen("chatwoot:999"))
        self.assertEqual(legacy.get_setting("last_chatwoot_matrix_event_id"), "$mx")

    def test_outgoing_without_matrix_event_id_fails_and_is_not_deduplicated(self):
        self.insert_link(conversation=123)
        payload = {
            "event": "message_created", "id": 999, "message_type": "outgoing", "private": False,
            "content": "hello", "conversation": {"id": 123, "inbox_id": 2},
        }
        with patch.object(legacy, "send_matrix_message", return_value={"ok": True, "ignored": True, "reason": "wrong inbox"}):
            with self.assertRaisesRegex(RuntimeError, "did not acknowledge"):
                module.handle_chatwoot_outgoing_verified(payload, signature_verified=True)
        self.assertFalse(legacy.event_seen("chatwoot:999"))

    def test_hmac_fallback_authenticates_exact_chatwoot_message(self):
        room = self.insert_link(conversation=123)
        payload = {
            "event": "message_created", "id": 999, "message_type": "outgoing", "private": False,
            "content": "hello", "conversation": {"id": 123, "inbox_id": 2},
        }
        api_responses = [
            {"id": 123, "inbox_id": 2},
            {"payload": [{"id": 999, "content": "hello", "message_type": 1, "private": False}]},
        ]
        with patch.object(prod, "cw_get", side_effect=api_responses) as cw_get, \
             patch.object(legacy, "send_matrix_message", return_value={"event_id": "$mx"}), \
             patch.object(module, "verify_matrix_event"):
            result = module.handle_chatwoot_outgoing_verified(payload, signature_verified=False)
        self.assertEqual(result["matrix_event_id"], "$mx")
        self.assertEqual(cw_get.call_count, 2)
        self.assertTrue(legacy.event_seen("chatwoot:999"))

    def test_stale_callback_timestamp_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "too old"):
            module._fresh_callback_timestamp("1000", now=2000)


if __name__ == "__main__":
    unittest.main()
