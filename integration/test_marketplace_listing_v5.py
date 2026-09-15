import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml


class MarketplaceListingV5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-listing-v5-tests"
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
                "namespaces": {"users": [], "aliases": [], "rooms": []},
            }, fh)
        with sqlite3.connect(os.environ["MAUTRIX_META_DB_PATH"]) as conn:
            conn.executescript("""
                CREATE TABLE user_login (bridge_id TEXT, user_mxid TEXT, id TEXT);
                CREATE TABLE portal (bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT, parent_id TEXT, name TEXT, metadata TEXT);
                CREATE TABLE message (bridge_id TEXT, id TEXT, part_id TEXT, mxid TEXT, room_id TEXT, room_receiver TEXT, sender_id TEXT, sender_mxid TEXT, timestamp INTEGER);
            """)
            conn.execute("INSERT INTO user_login VALUES(?,?,?)", ("meta", os.environ["MATRIX_ADMIN_MXID"], "111"))
            conn.execute(
                "INSERT INTO portal VALUES(?,?,?,?,?,?,?)",
                ("meta", "555", "111", "!market:matrix.example.com", "999", "Alberto · Hp core i3", json.dumps({"thread_type": 5})),
            )

        global module, base, media, enhancements, delivery, legacy, prod
        module = importlib.import_module("marketplace_listing_v5")
        base = importlib.import_module("marketplace_rebuild_v4")
        media = importlib.import_module("media_context_v3")
        enhancements = importlib.import_module("runtime_enhancements")
        delivery = importlib.import_module("delivery_history_v2")
        runtime = importlib.import_module("final_app")
        legacy = runtime.legacy
        prod = runtime.prod
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        module._LISTING_CACHE.clear()
        module._SCHEMA_CACHE.clear()
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")
        legacy.set_setting("chatwoot_base_url", "https://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "secret-token")
        legacy.set_setting("history_import_days", "365")

    def test_canonicalizes_real_marketplace_item_and_rejects_other_links(self):
        self.assertEqual(
            module._canonical_marketplace_url(
                "https://m.facebook.com/marketplace/item/123456789/?ref=messenger_share#foo"
            ),
            "https://www.facebook.com/marketplace/item/123456789/",
        )
        self.assertEqual(module._canonical_marketplace_url("https://www.facebook.com/profile.php?id=123"), "")
        self.assertEqual(module._canonical_marketplace_url("https://example.com/marketplace/item/123"), "")

    def test_extracts_xma_listing_link_title_and_thumbnail_from_nested_matrix_content(self):
        event = {
            "event_id": "$listing",
            "origin_server_ts": 1_700_000_000_000,
            "sender": "@meta_222:matrix.example.com",
            "content": {
                "msgtype": "m.image",
                "url": "mxc://matrix.example.com/item-image",
                "body": "image.jpg",
                "external_url": "https://www.facebook.com/marketplace/item/123456789/?ref=messenger",
                "com.beeper.meta.full_post_title": "Hp core i3",
            },
        }
        candidate = module._event_listing_candidate(event, "Hp core i3")
        self.assertEqual(candidate["url"], "https://www.facebook.com/marketplace/item/123456789/")
        self.assertEqual(candidate["title"], "Hp core i3")
        self.assertEqual(candidate["thumbnail_mxc"], "mxc://matrix.example.com/item-image")
        self.assertGreaterEqual(candidate["score"], 195)

    def test_listing_discovery_is_direction_agnostic_for_buyer_and_seller(self):
        incoming = {
            "event_id": "$buyer",
            "sender": "@meta_222:matrix.example.com",
            "content": {"external_url": "https://facebook.com/marketplace/item/777/", "com.beeper.meta.full_post_title": "Hp core i3"},
        }
        outgoing = {
            "event_id": "$seller",
            "sender": legacy.MATRIX_ADMIN_MXID,
            "content": {"external_url": "https://facebook.com/marketplace/item/777/", "com.beeper.meta.full_post_title": "Hp core i3"},
        }
        self.assertEqual(
            module._event_listing_candidate(incoming, "Hp core i3")["url"],
            module._event_listing_candidate(outgoing, "Hp core i3")["url"],
        )

    def test_discovery_prefers_title_matching_origin_card_over_newer_shared_listing(self):
        now_ms = 1_800_000_000_000
        newer_other = {
            "event_id": "$other", "origin_server_ts": now_ms,
            "content": {"external_url": "https://facebook.com/marketplace/item/999/", "com.beeper.meta.full_post_title": "Other item"},
        }
        older_origin = {
            "event_id": "$origin", "origin_server_ts": now_ms - 10_000,
            "content": {"external_url": "https://facebook.com/marketplace/item/777/", "com.beeper.meta.full_post_title": "Hp core i3"},
        }
        response = Mock()
        response.json.return_value = {"chunk": [newer_other, older_origin], "end": ""}
        with patch.object(enhancements, "_matrix_get", return_value=response), \
             patch.object(delivery, "history_days", return_value=3650):
            candidate = module.discover_marketplace_listing("!market:matrix.example.com", expected_title="Hp core i3")
        self.assertEqual(candidate["url"], "https://www.facebook.com/marketplace/item/777/")
        self.assertEqual(candidate["event_id"], "$origin")

    def test_schema_adds_single_operator_link_field(self):
        writes = []
        with patch.object(base, "ensure_chatwoot_marketplace_schema"), \
             patch.object(prod, "cw_get", return_value=[]), \
             patch.object(media, "_cw_json", side_effect=lambda method, path, payload=None: writes.append((method, path, payload)) or {}):
            module.ensure_listing_schema()
        payload = writes[0][2]
        self.assertEqual(payload["attribute_key"], "marketplace_listing_url")
        self.assertEqual(payload["attribute_display_name"], "Open Marketplace listing")
        self.assertEqual(payload["attribute_display_type"], 4)
        self.assertEqual(payload["attribute_model"], 0)

    def test_context_places_link_beside_title_and_thumbnail_as_private_note(self):
        candidate = {
            "url": "https://www.facebook.com/marketplace/item/777/",
            "title": "Hp core i3",
            "thumbnail_mxc": "mxc://matrix.example.com/item-image",
            "event_id": "$origin",
            "score": 195,
        }
        writes = []
        fake_response = Mock()
        fake_response.raise_for_status.return_value = None
        link = {"conversation_id": 117, "contact_id": 33}

        def fake_get(path):
            if path.endswith("/conversations/117"):
                return {"custom_attributes": {"marketplace_listing_title": "Hp core i3", "ai_mode": "human"}}
            if path.endswith("/conversations/117/messages"):
                return {"payload": []}
            raise AssertionError(path)

        with patch.object(base, "sync_conversation_context"), \
             patch.object(module, "ensure_listing_schema"), \
             patch.object(module, "discover_marketplace_listing", return_value=candidate), \
             patch.object(prod, "cw_get", side_effect=fake_get), \
             patch.object(media, "_cw_json", side_effect=lambda method, path, payload=None: writes.append((method, path, payload)) or {}), \
             patch.object(media, "download_matrix_media", return_value=(b"img", "item.jpg", "image/jpeg", "")), \
             patch.object(module.requests, "post", return_value=fake_response) as post:
            module.sync_conversation_context("!market:matrix.example.com", link)

        attrs = next(item[2]["custom_attributes"] for item in writes if item[1].endswith("/custom_attributes"))
        self.assertEqual(attrs["marketplace_listing_title"], "Hp core i3")
        self.assertEqual(attrs["marketplace_listing_url"], "https://www.facebook.com/marketplace/item/777/")
        self.assertEqual(attrs["ai_mode"], "human")
        self.assertEqual(post.call_args.kwargs["data"]["private"], "true")
        self.assertEqual(post.call_args.kwargs["data"]["content"], "")
        marker = json.loads(post.call_args.kwargs["data"]["content_attributes"])
        self.assertTrue(marker[module.LISTING_CARD_MARKER])

    def test_thumbnail_is_idempotent(self):
        candidate = {"url": "https://www.facebook.com/marketplace/item/777/", "thumbnail_mxc": "mxc://matrix.example.com/item"}
        existing = {"payload": [{"content_attributes": {module.LISTING_CARD_MARKER: True, "marketplace_listing_url": candidate["url"]}}]}
        with patch.object(prod, "cw_get", return_value=existing), \
             patch.object(media, "download_matrix_media") as download, \
             patch.object(module.requests, "post") as post:
            module._post_listing_thumbnail(1, 117, candidate)
        download.assert_not_called()
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
