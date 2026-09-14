import importlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import yaml


class MediaContextV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-media-context-tests"
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
                    "users": [{"regex": r"^@meta_[0-9]+:matrix\.example\.com$", "exclusive": True}],
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

        global runtime, module, hardening, legacy, prod, enhancements
        runtime = importlib.import_module("final_app")
        module = importlib.import_module("media_context_v3")
        hardening = importlib.import_module("media_context_v3_hardening")
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
        legacy.set_setting("history_import_days", "30")
        legacy.set_setting("chatwoot_enabled_at_ms", "1")

        with sqlite3.connect(module.META_DB_PATH) as conn:
            conn.execute("DELETE FROM message")
            conn.execute("DELETE FROM portal")
            conn.execute("DELETE FROM user_login")
            conn.execute(
                "INSERT INTO user_login(bridge_id, user_mxid, id) VALUES(?, ?, ?)",
                ("meta", legacy.MATRIX_ADMIN_MXID, "111"),
            )
            conn.execute(
                "INSERT INTO portal(bridge_id, id, receiver, mxid, parent_id, name, metadata) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                ("meta", "555", "111", "!market:matrix.example.com", "999", "Marketplace chat", json.dumps({"thread_type": 5})),
            )

    def add_message(self, mxid, sender_id, sender_mxid, timestamp=2_000_000_000_000):
        with sqlite3.connect(module.META_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO message(bridge_id,id,part_id,mxid,room_id,room_receiver,sender_id,sender_mxid,timestamp) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("meta", "fb:" + mxid, "", mxid, "555", "111", str(sender_id), sender_mxid, timestamp),
            )

    def insert_link(self, conversation=77):
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?,?,?,?,?)",
                ("!market:matrix.example.com", 5, "source-5", conversation, int(time.time())),
            )

    def test_mautrix_database_is_authoritative_for_historical_direction(self):
        self.add_message("$mine", "111", "@meta_111:matrix.example.com")
        self.add_message("$theirs", "222", "@meta_222:matrix.example.com")
        mine = {"event_id": "$mine", "sender": "@meta_111:matrix.example.com"}
        theirs = {"event_id": "$theirs", "sender": "@meta_222:matrix.example.com"}
        self.assertEqual(module.message_direction(mine), "outgoing")
        self.assertEqual(module.message_direction(theirs), "incoming")

    def test_history_import_mirrors_both_sides_with_correct_direction(self):
        now = 2_000_000_000
        self.add_message("$mine", "111", "@meta_111:matrix.example.com", int((now - 10) * 1000))
        self.add_message("$theirs", "222", "@meta_222:matrix.example.com", int((now - 20) * 1000))
        mine = {
            "event_id": "$mine", "type": "m.room.message", "sender": "@meta_111:matrix.example.com",
            "origin_server_ts": int((now - 10) * 1000), "content": {"msgtype": "m.text", "body": "my old reply"},
        }
        theirs = {
            "event_id": "$theirs", "type": "m.room.message", "sender": "@meta_222:matrix.example.com",
            "origin_server_ts": int((now - 20) * 1000), "content": {"msgtype": "m.text", "body": "customer"},
        }
        response = Mock(); response.json.return_value = {"chunk": [mine, theirs]}
        directions = []

        def capture_text(conversation_id, *, body, direction, event_id, history):
            directions.append((event_id, direction, history))
            return {"id": len(directions)}

        with patch.object(module.time, "time", return_value=now), \
             patch.object(enhancements, "_matrix_get", return_value=response), \
             patch.object(prod, "is_bridge_portal", return_value=True), \
             patch.object(prod, "ensure_room_link", return_value={"conversation_id": 77}), \
             patch.object(module, "sync_conversation_context"), \
             patch.object(module, "_post_chatwoot_text", side_effect=capture_text):
            imported = module.import_recent_history("!market:matrix.example.com")

        self.assertEqual(imported, 2)
        self.assertEqual(directions, [
            ("$theirs", "incoming", True),
            ("$mine", "outgoing", True),
        ])

    def test_marketplace_context_propagates_to_existing_conversation_and_preserves_labels(self):
        self.insert_link(conversation=77)
        link = {"conversation_id": 77}
        get_responses = [
            {"id": 77, "custom_attributes": {"existing": "keep"}},
            {"payload": ["vip"]},
        ]
        with patch.object(prod, "cw_get", side_effect=get_responses), \
             patch.object(module, "_cw_json", return_value={}) as write:
            hardening._original_context_sync("!market:matrix.example.com", link)

        attr_payload = write.call_args_list[0].args[2]["custom_attributes"]
        label_payload = write.call_args_list[1].args[2]["labels"]
        self.assertEqual(attr_payload["existing"], "keep")
        self.assertEqual(attr_payload["meta_channel"], "facebook_marketplace")
        self.assertEqual(attr_payload["meta_thread_type"], 5)
        self.assertEqual(attr_payload["meta_parent_portal_id"], "999")
        self.assertEqual(set(label_payload), {"vip", "facebook", "marketplace"})

    def test_external_attachment_url_never_receives_chatwoot_api_token(self):
        with patch.object(hardening, "_original_bounded_download", return_value=(b"x", "image/png")) as download:
            hardening.bounded_download(
                "https://objects.example.net/signed/photo.png",
                headers={"api_access_token": "secret-token"},
            )
        self.assertEqual(download.call_args.kwargs["headers"], {})

    def test_same_origin_attachment_can_use_chatwoot_api_token(self):
        with patch.object(hardening, "_original_bounded_download", return_value=(b"x", "image/png")) as download:
            hardening.bounded_download(
                "https://chatwoot.example.com/rails/active_storage/photo.png",
                headers={"api_access_token": "secret-token"},
            )
        self.assertEqual(download.call_args.kwargs["headers"]["api_access_token"], "secret-token")

    def test_string_mirror_marker_suppresses_chatwoot_replay(self):
        payload = {
            "event": "message_created", "id": 91, "message_type": "outgoing", "private": False,
            "content": "mirrored", "content_attributes": {module.MIRROR_MARKER: "true"},
            "conversation": {"id": 77, "inbox_id": 2},
        }
        with patch.object(hardening, "_original_outgoing") as original:
            result = hardening.handle_chatwoot_outgoing(payload, signature_verified=True)
        self.assertEqual(result.get("reason"), "matrix_mirror")
        original.assert_not_called()

    def test_attachment_only_chatwoot_message_uploads_and_sends_matrix_media(self):
        self.insert_link(conversation=123)
        payload = {
            "event": "message_created", "id": 999, "message_type": "outgoing", "private": False,
            "content": "", "attachments": [{"data_url": "https://objects.example.net/p.png", "file_name": "p.png", "content_type": "image/png"}],
            "conversation": {"id": 123, "inbox_id": 2},
        }
        with patch.object(runtime, "room_matches_configured_chatwoot_inbox", return_value=(True, 123, 2)), \
             patch.object(module, "_bounded_download", return_value=(b"png", "image/png")), \
             patch.object(module, "_matrix_upload", return_value="mxc://matrix.example.com/media"), \
             patch.object(module, "_matrix_send", return_value="$media") as send, \
             patch.object(module, "_verify_matrix_content") as verify:
            result = module.handle_chatwoot_outgoing(payload, signature_verified=True)

        sent_content = send.call_args.args[2]
        self.assertEqual(sent_content["msgtype"], "m.image")
        self.assertEqual(sent_content["url"], "mxc://matrix.example.com/media")
        self.assertEqual(result["matrix_event_id"], "$media")
        verify.assert_called_once()
        self.assertTrue(legacy.event_seen("chatwoot:999"))
        self.assertTrue(legacy.event_seen("$media"))

    def test_matrix_image_is_mirrored_to_chatwoot_attachment(self):
        self.add_message("$image", "222", "@meta_222:matrix.example.com")
        event = {
            "event_id": "$image", "type": "m.room.message", "sender": "@meta_222:matrix.example.com",
            "content": {"msgtype": "m.image", "body": "photo.png", "url": "mxc://matrix.example.com/media", "info": {"mimetype": "image/png"}},
        }
        with patch.object(prod, "is_bridge_portal", return_value=True), \
             patch.object(prod, "ensure_room_link", return_value={"conversation_id": 77}), \
             patch.object(module, "sync_conversation_context"), \
             patch.object(module, "download_matrix_media", return_value=(b"png", "photo.png", "image/png", "")), \
             patch.object(module, "post_chatwoot_media", return_value={"id": 44}) as post:
            self.assertTrue(module.mirror_matrix_event("!market:matrix.example.com", event, history=False))
        self.assertEqual(post.call_args.kwargs["direction"], "incoming")
        self.assertTrue(legacy.event_seen("$image"))


if __name__ == "__main__":
    unittest.main()
