import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import yaml


class MarketplaceRebuildV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-marketplace-rebuild-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        os.environ["MAUTRIX_META_DB_PATH"] = os.path.join(cls.tmp.name, "mautrix-meta.db")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            yaml.safe_dump({
                "id": "meta",
                "sender_localpart": "metabot",
                "namespaces": {
                    "users": [{"regex": r"^@meta_[0-9]+:matrix\\.example\\.com$", "exclusive": True}],
                    "aliases": [],
                    "rooms": [],
                },
            }, fh, sort_keys=False)

        with sqlite3.connect(os.environ["MAUTRIX_META_DB_PATH"]) as conn:
            conn.executescript("""
                CREATE TABLE user_login (
                    bridge_id TEXT NOT NULL,
                    user_mxid TEXT NOT NULL,
                    id TEXT NOT NULL
                );
                CREATE TABLE portal (
                    bridge_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    mxid TEXT,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    metadata TEXT NOT NULL
                );
                CREATE TABLE message (
                    bridge_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    part_id TEXT NOT NULL,
                    mxid TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    room_receiver TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_mxid TEXT NOT NULL,
                    timestamp INTEGER NOT NULL
                );
            """)

        global runtime, module, media, enhancements, legacy, prod
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("marketplace_rebuild_v4")
        media = importlib.import_module("media_context_v3")
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
        legacy.set_setting("chatwoot_base_url", "https://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "secret-token")
        legacy.set_setting("import_history_on_join", "1")
        legacy.set_setting("history_import_days", "365")
        module._SCHEMA_CACHE.clear()

        with sqlite3.connect(media.META_DB_PATH) as conn:
            conn.execute("DELETE FROM message")
            conn.execute("DELETE FROM portal")
            conn.execute("DELETE FROM user_login")
            conn.execute(
                "INSERT INTO user_login(bridge_id,user_mxid,id) VALUES(?,?,?)",
                ("meta", legacy.MATRIX_ADMIN_MXID, "111"),
            )
            conn.execute(
                "INSERT INTO portal(bridge_id,id,receiver,mxid,parent_id,name,metadata) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    "meta", "555", "111", "!market:matrix.example.com", "999",
                    "Alberto · Hp core i3", json.dumps({"thread_type": 5}),
                ),
            )
            conn.execute(
                "INSERT INTO message(bridge_id,id,part_id,mxid,room_id,room_receiver,sender_id,sender_mxid,timestamp) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "meta", "fb-old", "", "$old", "555", "111", "222222222",
                    "@meta_222222222:matrix.example.com", 1_700_000_000_000,
                ),
            )

    def test_missing_link_resets_room_dedupe_before_reimport(self):
        legacy.mark_event("$old", "matrix_to_chatwoot")
        legacy.mark_event("$unrelated", "matrix_to_chatwoot")

        observed = {}

        def fake_import(room_id):
            observed["old_seen"] = legacy.event_seen("$old")
            observed["unrelated_seen"] = legacy.event_seen("$unrelated")
            return 4

        with patch.object(module, "_base_import_history", side_effect=fake_import), \
             patch.object(module, "_base_repair_deleted", return_value=False):
            imported = module.import_recent_history("!market:matrix.example.com")

        self.assertEqual(imported, 4)
        self.assertFalse(observed["old_seen"])
        self.assertTrue(observed["unrelated_seen"])

    def test_unseen_admin_puppet_event_is_mirrored_immediately(self):
        event = {
            "event_id": "$facebook-self",
            "type": "m.room.message",
            "sender": legacy.MATRIX_ADMIN_MXID,
            "content": {"msgtype": "m.text", "body": "todavia esta disponible"},
        }
        with patch.object(module, "_base_mirror_event", return_value=True) as mirror:
            self.assertTrue(module.mirror_matrix_event("!market:matrix.example.com", event))
        self.assertTrue(mirror.call_args.kwargs["history"])

    def test_chatwoot_origin_admin_event_keeps_live_loop_guard(self):
        legacy.mark_event("$chatwoot", "chatwoot_to_matrix_origin")
        event = {
            "event_id": "$chatwoot",
            "type": "m.room.message",
            "sender": legacy.MATRIX_ADMIN_MXID,
            "content": {"msgtype": "m.text", "body": "agent reply"},
        }
        with patch.object(module, "_base_mirror_event", return_value=False) as mirror:
            self.assertFalse(module.mirror_matrix_event("!market:matrix.example.com", event))
        self.assertFalse(mirror.call_args.kwargs["history"])

    def test_marketplace_exposes_listing_without_duplicating_contact_name(self):
        title = module._marketplace_listing_title({
            "name": "Alberto · Hp core i3",
            "is_marketplace": True,
        })
        self.assertEqual(title, "Hp core i3")

    def test_facebook_profile_url_uses_numeric_remote_user_id(self):
        self.assertEqual(
            module.facebook_profile_url("!market:matrix.example.com"),
            "https://www.facebook.com/profile.php?id=222222222",
        )
        self.assertEqual(module._facebook_numeric_id("100045218265910:4@msgr"), "100045218265910")
        self.assertEqual(module._facebook_numeric_id("not-a-facebook-id"), "")

    def test_schema_keeps_only_operator_fields_and_marketplace_label(self):
        definitions = [
            {
                "id": 10,
                "attribute_key": "matrix_room_id",
                "attribute_description": module._OWNED_DESCRIPTION,
            },
            {
                "id": 11,
                "attribute_key": "custom_user_field",
                "attribute_description": "Created by operator",
            },
        ]
        labels = [
            {
                "id": 20,
                "title": "facebook",
                "description": "Conversation bridged from Facebook",
            }
        ]
        responses = [definitions, labels]
        writes = []

        def fake_get(path):
            return responses.pop(0)

        def fake_write(method, path, payload=None):
            writes.append((method, path, payload))
            return {}

        with patch.object(prod, "cw_get", side_effect=fake_get), \
             patch.object(media, "_cw_json", side_effect=fake_write):
            module.ensure_chatwoot_marketplace_schema()

        created_keys = {
            call[2].get("attribute_key")
            for call in writes
            if call[0] == "POST" and "/custom_attribute_definitions" in call[1]
        }
        deleted_paths = {call[1] for call in writes if call[0] == "DELETE"}
        created_labels = {
            call[2].get("title")
            for call in writes
            if call[0] == "POST" and call[1].endswith("/labels")
        }

        self.assertEqual(created_keys, {"marketplace_listing_title", "facebook_profile_url"})
        self.assertIn("/api/v1/accounts/1/custom_attribute_definitions/10", deleted_paths)
        self.assertNotIn("/api/v1/accounts/1/custom_attribute_definitions/11", deleted_paths)
        self.assertIn("/api/v1/accounts/1/labels/20", deleted_paths)
        self.assertEqual(created_labels, {"marketplace"})

    def test_context_sync_removes_bridge_plumbing_and_sets_useful_fields(self):
        link = {"conversation_id": 117, "contact_id": 33}
        conversation = {
            "custom_attributes": {
                "matrix_room_id": "!market:matrix.example.com",
                "meta_channel": "facebook_marketplace",
                "marketplace_counterparty_name": "Alberto",
                "ai_mode": "human",
            }
        }
        calls = []

        def fake_get(path):
            if path.endswith("/conversations/117"):
                return conversation
            if path.endswith("/conversations/117/labels"):
                return {"payload": ["facebook", "lead_caliente"]}
            raise AssertionError(path)

        def fake_write(method, path, payload=None):
            calls.append((method, path, payload))
            return {}

        with patch.object(module, "ensure_chatwoot_marketplace_schema"), \
             patch.object(prod, "cw_get", side_effect=fake_get), \
             patch.object(media, "_cw_json", side_effect=fake_write), \
             patch.object(enhancements, "chatwoot_request") as contact_update:
            module.sync_conversation_context("!market:matrix.example.com", link)

        attrs_payload = next(
            call[2]["custom_attributes"] for call in calls
            if call[1].endswith("/custom_attributes")
        )
        self.assertEqual(
            attrs_payload,
            {"ai_mode": "human", "marketplace_listing_title": "Hp core i3"},
        )
        labels_payload = next(call[2]["labels"] for call in calls if call[1].endswith("/labels"))
        self.assertEqual(labels_payload, ["lead_caliente", "marketplace"])
        contact_update.assert_called_once()
        self.assertEqual(
            contact_update.call_args.kwargs["json"]["custom_attributes"]["facebook_profile_url"],
            "https://www.facebook.com/profile.php?id=222222222",
        )


if __name__ == "__main__":
    unittest.main()
