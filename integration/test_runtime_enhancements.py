import importlib
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import requests
import yaml


class RuntimeEnhancementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-runtime-enhancement-tests"
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
                    "users": [
                        {"regex": r"^@meta_[0-9]+:matrix\.example\.com$", "exclusive": True},
                    ],
                    "aliases": [],
                    "rooms": [],
                },
            }, fh, sort_keys=False)
        global module, runtime, legacy, prod
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("runtime_enhancements")
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
        legacy.set_setting("chatwoot_enabled_at_ms", "1")
        legacy.set_setting("auto_join_meta_portals", "1")
        legacy.set_setting("import_history_on_join", "1")
        legacy.set_setting("history_import_limit", "100")
        legacy.set_setting("sync_contact_profiles", "1")
        legacy.set_setting("repair_deleted_conversations", "1")

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
        membership = Mock()
        membership.content = b'{"membership":"join"}'
        membership.json.return_value = {"membership": "join"}
        with patch.object(module, "_matrix_post") as join, \
             patch.object(module, "_matrix_get", return_value=membership):
            self.assertTrue(module.auto_join_room("!new:matrix.example.com", self.invite()))
        join.assert_called_once_with("/_matrix/client/v3/join/%21new%3Amatrix.example.com")

    def test_arbitrary_matrix_invite_is_never_auto_joined(self):
        with patch.object(module, "_matrix_post") as join:
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
        with patch.object(module, "contact_identity", return_value={"name": profile["displayname"], "avatar_url": ""}), \
             patch.object(module, "update_chatwoot_contact_profile"), \
             patch.object(legacy, "cw_post", side_effect=posts) as cw_post:
            link = module.enhanced_ensure_room_link("!new:matrix.example.com", "@meta_123:matrix.example.com")
        self.assertEqual(link["conversation_id"], 77)
        self.assertEqual(cw_post.call_args_list[0].args[1]["name"], "Julio Alberto Marte Balbuena")

    def test_existing_link_refreshes_profile_without_breaking_delivery(self):
        room = self.insert_link()
        with patch.object(module, "repair_deleted_conversation", return_value=False), \
             patch.object(module, "update_chatwoot_contact_profile") as profile_sync:
            row = module.enhanced_ensure_room_link(room, "@meta_123:matrix.example.com")
        self.assertEqual(row["conversation_id"], 77)
        profile_sync.assert_called_once_with(1, 5, "@meta_123:matrix.example.com")

    def test_deleted_chatwoot_conversation_removes_stale_mapping(self):
        room = self.insert_link()
        with patch.object(prod, "cw_get", side_effect=self.http_error(404)):
            self.assertTrue(module.repair_deleted_conversation(room))
        self.assertIsNone(module.existing_room_link(room))

    def test_non_404_does_not_delete_mapping(self):
        room = self.insert_link()
        with patch.object(prod, "cw_get", side_effect=self.http_error(500)):
            with self.assertRaises(requests.HTTPError):
                module.repair_deleted_conversation(room)
        self.assertIsNotNone(module.existing_room_link(room))

    def test_history_import_is_oldest_first_and_deduplicated(self):
        events = [
            {"event_id": "$new", "type": "m.room.message", "sender": "@meta_2:matrix.example.com", "content": {"body": "new"}},
            {"event_id": "$old", "type": "m.room.message", "sender": "@meta_2:matrix.example.com", "content": {"body": "old"}},
        ]
        response = Mock()
        response.json.return_value = {"chunk": events}
        seen = set()
        ordered = []

        def fake_seen(event_id):
            return event_id in seen

        def fake_bridge(room_id, event):
            ordered.append(event["event_id"])
            seen.add(event["event_id"])

        with patch.object(module, "_matrix_get", return_value=response), \
             patch.object(legacy, "event_seen", side_effect=fake_seen), \
             patch.object(prod, "matrix_event_to_chatwoot", side_effect=fake_bridge):
            self.assertEqual(module.import_recent_history("!room:matrix.example.com"), 2)
            self.assertEqual(module.import_recent_history("!room:matrix.example.com"), 0)
        self.assertEqual(ordered, ["$old", "$new", "$old", "$new"])

    def test_history_import_ignores_non_message_events(self):
        response = Mock()
        response.json.return_value = {"chunk": [{"event_id": "$member", "type": "m.room.member"}]}
        with patch.object(module, "_matrix_get", return_value=response), \
             patch.object(prod, "matrix_event_to_chatwoot") as bridge:
            self.assertEqual(module.import_recent_history("!room:matrix.example.com"), 0)
        bridge.assert_not_called()

    def test_api_inbox_signature_accepts_valid_delivery(self):
        secret = "api-inbox-secret"
        legacy.set_setting("chatwoot_api_inbox_signing_secret", secret)
        raw = b'{"event":"message_created"}'
        ts = "1789350000"
        signature = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        self.assertTrue(module.verify_inbox_signature(raw, signature, ts, now=1789350000))

    def test_api_inbox_signature_rejects_invalid_signature(self):
        legacy.set_setting("chatwoot_api_inbox_signing_secret", "api-inbox-secret")
        with self.assertRaises(RuntimeError):
            module.verify_inbox_signature(b"{}", "sha256=bad", "1789350000", now=1789350000)

    def test_api_inbox_callback_configuration_uses_selected_inbox(self):
        details = {
            "id": 2,
            "channel_type": "Channel::Api",
            "webhook_url": "https://bridge.example.com/webhooks/chatwoot/inbox",
            "secret": "api-secret",
        }
        with patch.object(module, "chatwoot_request") as request_call, \
             patch.object(module, "api_inbox_details", return_value=details):
            result = module.configure_api_inbox_callback("https://bridge.example.com/webhooks/chatwoot/inbox")
        request_call.assert_called_once()
        self.assertIn("verified", result)
        self.assertEqual(legacy.get_setting("chatwoot_api_inbox_signing_secret"), "api-secret")

    def test_outgoing_callback_deduplicates_same_chatwoot_message(self):
        room = self.insert_link(conversation=123)
        payload = {
            "event": "message_created",
            "id": 999,
            "message_type": "outgoing",
            "private": False,
            "content": "hello",
            "conversation": {"id": 123, "inbox_id": 2},
        }
        with patch.object(legacy, "send_matrix_message") as send:
            first = module.handle_chatwoot_outgoing(payload)
            second = module.handle_chatwoot_outgoing(payload)
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("duplicate"))
        send.assert_called_once_with(room, "hello", "cw-999")

    def test_outgoing_callback_rejects_other_inbox(self):
        self.insert_link(conversation=123)
        payload = {
            "event": "message_created",
            "id": 999,
            "message_type": "outgoing",
            "private": False,
            "content": "hello",
            "conversation": {"id": 123, "inbox_id": 9},
        }
        with patch.object(legacy, "send_matrix_message") as send:
            result = module.handle_chatwoot_outgoing(payload)
        self.assertEqual(result.get("reason"), "outside_configured_chatwoot_inbox")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
