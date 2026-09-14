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
                    "meta", "fb-old", "", "$old", "555", "111", "222",
                    "@meta_222:matrix.example.com", 1_700_000_000_000,
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

    def test_marketplace_room_name_exposes_listing_and_counterparty(self):
        attrs = module._marketplace_name_attributes({
            "name": "Alberto · Hp core i3",
            "is_marketplace": True,
        })
        self.assertEqual(attrs["meta_thread_name"], "Alberto · Hp core i3")
        self.assertEqual(attrs["marketplace_counterparty_name"], "Alberto")
        self.assertEqual(attrs["marketplace_listing_title"], "Hp core i3")

    def test_schema_registration_creates_visible_attributes_and_labels(self):
        module._SCHEMA_CACHE.clear()
        responses = [[], []]
        writes = []

        def fake_get(path):
            return responses.pop(0)

        def fake_write(method, path, payload=None):
            writes.append((method, path, payload))
            return {}

        with patch.object(prod, "cw_get", side_effect=fake_get), \
             patch.object(media, "_cw_json", side_effect=fake_write):
            module.ensure_chatwoot_marketplace_schema()

        keys = {
            call[2].get("attribute_key")
            for call in writes
            if "/custom_attribute_definitions" in call[1]
        }
        labels = {
            call[2].get("title")
            for call in writes
            if call[1].endswith("/labels") and "title" in call[2]
        }
        self.assertIn("marketplace_listing_title", keys)
        self.assertIn("meta_thread_name", keys)
        self.assertEqual(labels, {"facebook", "marketplace"})


if __name__ == "__main__":
    unittest.main()
