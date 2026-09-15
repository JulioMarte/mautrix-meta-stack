import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import yaml


class SyncIntegrityV6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-v6-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        os.environ["MAUTRIX_META_DB_PATH"] = os.path.join(cls.tmp.name, "mautrix-meta.db")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            yaml.safe_dump({"id": "meta", "sender_localpart": "metabot", "namespaces": {"users": [], "aliases": [], "rooms": []}}, fh)
        with sqlite3.connect(os.environ["MAUTRIX_META_DB_PATH"]) as conn:
            conn.executescript("""
                CREATE TABLE user_login (bridge_id TEXT, user_mxid TEXT, id TEXT);
                CREATE TABLE portal (bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT, parent_id TEXT, name TEXT, metadata TEXT);
                CREATE TABLE message (bridge_id TEXT, id TEXT, part_id TEXT, mxid TEXT, room_id TEXT, room_receiver TEXT, sender_id TEXT, sender_mxid TEXT, timestamp INTEGER);
            """)
            conn.execute("INSERT INTO user_login VALUES(?,?,?)", ("meta", os.environ["MATRIX_ADMIN_MXID"], "111"))
            conn.execute(
                "INSERT INTO portal VALUES(?,?,?,?,?,?,?)",
                ("meta", "4353439204919012", "111", "!market:matrix.example.com", "-12", "Alberto · Silla de escritorio", json.dumps({"thread_type": 5})),
            )
            conn.executemany(
                "INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("meta", "m1", "", "$cw", "4353439204919012", "111", "111", "@admin:matrix.example.com", 1000),
                    ("meta", "m2", "", "$remote", "4353439204919012", "111", "222", "@meta_222:matrix.example.com", 2000),
                ],
            )

        importlib.import_module("final_app")
        global module, runtime, hardening
        module = importlib.import_module("sync_integrity_v6")
        runtime = importlib.import_module("final_app")
        hardening = importlib.import_module("media_context_v3_hardening")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_falls_back_to_authoritative_facebook_thread_when_item_url_missing(self):
        with patch.object(module.listing, "discover_marketplace_listing", return_value={}):
            url, kind = module._best_marketplace_url("!market:matrix.example.com", "Silla de escritorio")
        self.assertEqual(kind, "thread")
        self.assertEqual(url, "https://www.facebook.com/messages/t/4353439204919012/")

    def test_prefers_real_marketplace_item_url_over_thread_fallback(self):
        item = "https://www.facebook.com/marketplace/item/987654321/"
        with patch.object(module.listing, "discover_marketplace_listing", return_value={"url": item}):
            url, kind = module._best_marketplace_url("!market:matrix.example.com", "Silla de escritorio")
        self.assertEqual((url, kind), (item, "listing"))

    def test_room_rebuild_preserves_chatwoot_origin_event_but_resets_remote_import(self):
        with runtime.legacy.db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO processed_events(event_id, direction, created_at) VALUES(?,?,?)",
                ("$cw", "chatwoot_to_matrix_origin", 1),
            )
            conn.execute(
                "INSERT OR REPLACE INTO processed_events(event_id, direction, created_at) VALUES(?,?,?)",
                ("$remote", "matrix_to_chatwoot", 1),
            )
        removed = module.reset_room_chatwoot_dedupe("!market:matrix.example.com", reason="test")
        self.assertEqual(removed, 1)
        with runtime.legacy.db() as conn:
            cw = conn.execute("SELECT direction FROM processed_events WHERE event_id='$cw'").fetchone()
            remote = conn.execute("SELECT direction FROM processed_events WHERE event_id='$remote'").fetchone()
        self.assertIsNotNone(cw)
        self.assertEqual(cw["direction"], "chatwoot_to_matrix_origin")
        self.assertIsNone(remote)

    def test_external_created_at_uses_matrix_origin_timestamp(self):
        token = module._EVENT_TS_MS.set(1726354140000)
        try:
            value = module._external_created_at()
        finally:
            module._EVENT_TS_MS.reset(token)
        self.assertEqual(value, "2024-09-14T22:49:00Z")

    def test_text_only_chatwoot_reply_uses_origin_marking_delivery_path(self):
        payload = {
            "event": "message_created",
            "id": 7440,
            "message_type": "outgoing",
            "private": False,
            "content": "kkk",
            "content_attributes": {},
            "conversation": {"id": 176, "inbox_id": 12},
        }
        expected = {"ok": True, "matrix_event_id": "$matrix-origin"}
        with patch.object(hardening, "_original_media_outgoing", return_value=expected) as origin_path, \
             patch.object(hardening, "_legacy_text_only_outgoing") as legacy_path:
            result = hardening.handle_chatwoot_outgoing(payload, signature_verified=True)
        self.assertEqual(result, expected)
        origin_path.assert_called_once_with(payload, signature_verified=True)
        legacy_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
