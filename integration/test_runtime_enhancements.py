import hashlib
import hmac
import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

import requests


class RuntimeEnhancementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-runtime-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["META_PROXY_ENABLED"] = "false"
        os.environ.pop("META_PROXY_URL", None)
        global module, legacy, runtime
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("runtime_enhancements")
        legacy = runtime.legacy
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        module._profile_cache.clear()
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")

    @staticmethod
    def invite(inviter="@metabot:matrix.example.com"):
        return {
            "invite_state": {
                "events": [
                    {
                        "type": "m.room.member",
                        "state_key": "@admin:matrix.example.com",
                        "sender": inviter,
                        "content": {"membership": "invite"},
                    }
                ]
            }
        }

    def http_error(self, status):
        response = requests.Response()
        response.status_code = status
        response.url = "http://chatwoot.example.com/test"
        return requests.HTTPError(response=response)

    def insert_link(self, room="!portal:matrix.example.com", conversation=77, contact=5):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?, ?, ?, ?, ?)",
                (room, contact, "source-5", conversation, int(time.time())),
            )
        return room

    def test_trusted_metabot_invite_is_auto_joined(self):
        with patch.object(legacy, "bridge_bot_mxid", return_value="@metabot:matrix.example.com"), \
             patch.object(module, "_matrix_post") as join:
            self.assertTrue(module.auto_join_room("!new:matrix.example.com", self.invite()))
        join.assert_called_once_with("/_matrix/client/v3/join/%21new%3Amatrix.example.com")

    def test_arbitrary_matrix_invite_is_never_auto_joined(self):
        with patch.object(legacy, "bridge_bot_mxid", return_value="@metabot:matrix.example.com"), \
             patch.object(module, "_matrix_post") as join:
            self.assertFalse(module.auto_join_room("!evil:matrix.example.com", self.invite("@someone:matrix.example.com")))
        join.assert_not_called()

    def test_auto_join_can_be_disabled(self):
        legacy.set_setting("auto_join_meta_portals", "0")
        with patch.object(module, "_matrix_post") as join:
            self.assertFalse(module.auto_join_room("!new:matrix.example.com", self.invite()))
        join.assert_not_called()

    def test_real_matrix_display_name_is_used_for_contact(self):
        profile = {"displayname": "Julio Alberto Marte Balbuena", "avatar_url": ""}
        contact = {"id": 5, "contact_inboxes": []}
        posts = [contact, {"source_id": "source-5"}, {"id": 77}]
        with patch.object(module, "matrix_profile", return_value=profile), \
             patch.object(legacy, "cw_post", side_effect=posts) as post, \
             patch.object(module, "update_chatwoot_contact_profile"):
            link = module.enhanced_ensure_room_link("!new:matrix.example.com", "@meta_1250093139:matrix.example.com")
        self.assertEqual(link["conversation_id"], 77)
        self.assertEqual(post.call_args_list[0].args[1]["name"], "Julio Alberto Marte Balbuena")

    def test_profile_enrichment_failure_never_blocks_message_link(self):
        self.insert_link()
        with patch.object(module, "update_chatwoot_contact_profile", side_effect=None) as update:
            row = module.enhanced_ensure_room_link("!portal:matrix.example.com", "@meta_1:matrix.example.com")
        self.assertEqual(row["conversation_id"], 77)
        update.assert_called_once()

    def test_deleted_chatwoot_conversation_removes_stale_mapping(self):
        room = self.insert_link()
        with patch.object(module.prod, "cw_get", side_effect=self.http_error(404)):
            self.assertTrue(module.repair_deleted_conversation(room))
        self.assertIsNone(module.existing_room_link(room))

    def test_non_404_chatwoot_error_does_not_delete_mapping(self):
        room = self.insert_link()
        with patch.object(module.prod, "cw_get", side_effect=self.http_error(500)):
            with self.assertRaises(requests.HTTPError):
                module.repair_deleted_conversation(room)
        self.assertIsNotNone(module.existing_room_link(room))

    def test_history_import_processes_oldest_to_newest_and_is_idempotent(self):
        events = [
            {"event_id": "$new", "type": "m.room.message"},
            {"event_id": "$old", "type": "m.room.message"},
        ]
        response = Mock()
        response.json.return_value = {"chunk": events}

        def deliver(_room, event):
            legacy.mark_event(event["event_id"], "matrix_to_chatwoot")

        with patch.object(module, "_matrix_get", return_value=response), \
             patch.object(module.prod, "matrix_event_to_chatwoot", side_effect=deliver) as deliver_mock:
            count = module.import_recent_history("!portal:matrix.example.com")
        self.assertEqual(count, 2)
        self.assertEqual(
            [item.args[1]["event_id"] for item in deliver_mock.call_args_list],
            ["$old", "$new"],
        )

    def test_api_inbox_callback_verification_imports_independent_secret(self):
        with patch.object(module.prod, "cw_get", return_value={
            "id": 2,
            "channel_type": "Channel::Api",
            "webhook_url": "https://bridge.example.com/webhooks/chatwoot/inbox",
            "secret": "api-inbox-secret",
        }):
            result = module.verify_api_inbox_callback("https://bridge.example.com/webhooks/chatwoot/inbox")
        self.assertIn("PASS", result)
        self.assertEqual(legacy.get_setting("chatwoot_api_inbox_signing_secret"), "api-inbox-secret")
        self.assertTrue(legacy.get_setting("api_inbox_callback_verified_at"))

    def test_callback_configuration_updates_channel_webhook_url(self):
        expected = "https://bridge.example.com/webhooks/chatwoot/inbox"
        with patch.object(module, "chatwoot_request", return_value={}) as request_call, \
             patch.object(module, "verify_api_inbox_callback", return_value="PASS"):
            self.assertEqual(module.configure_api_inbox_callback(expected), "PASS")
        request_call.assert_called_once_with(
            "PATCH",
            "/api/v1/accounts/1/inboxes/2",
            json={"channel": {"webhook_url": expected}},
            headers={"Content-Type": "application/json"},
        )

    def test_api_inbox_signature_uses_inbox_secret(self):
        secret = "api-inbox-signing-secret"
        legacy.set_setting("chatwoot_api_inbox_signing_secret", secret)
        raw = b'{"event":"message_created"}'
        timestamp = str(int(time.time()))
        digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        self.assertTrue(module.verify_inbox_signature(raw, "sha256=" + digest, timestamp, now=int(timestamp)))
        with self.assertRaisesRegex(RuntimeError, "Invalid Chatwoot API inbox"):
            module.verify_inbox_signature(raw, "sha256=bad", timestamp, now=int(timestamp))

    def test_outgoing_callback_from_other_inbox_is_ignored(self):
        payload = {
            "event": "message_created", "id": 44, "message_type": "outgoing", "content": "hello",
            "inbox": {"id": 99}, "conversation": {"id": 77, "inbox_id": 99},
        }
        with patch.object(legacy, "send_matrix_message") as send:
            result = module.handle_chatwoot_outgoing(payload)
        self.assertTrue(result["ignored"])
        self.assertEqual(result["reason"], "outside_configured_chatwoot_inbox")
        send.assert_not_called()

    def test_duplicate_outgoing_message_id_is_not_sent_twice(self):
        room = self.insert_link()
        payload = {
            "event": "message_created", "id": 44, "message_type": "outgoing", "content": "hello",
            "inbox": {"id": 2}, "conversation": {"id": 77, "inbox_id": 2},
        }
        legacy.mark_event("chatwoot:44", "chatwoot_to_matrix")
        with patch.object(legacy, "send_matrix_message") as send:
            result = module.handle_chatwoot_outgoing(payload)
        self.assertTrue(result["duplicate"])
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
