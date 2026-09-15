import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml


class MarketplaceListingDiscoveryV6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-listing-v6-tests"
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
                ("meta", "555", "111", "!market:matrix.example.com", "999", "Yaneiri · Silla de escritorio", json.dumps({"thread_type": 5})),
            )

        global module, enhancements, delivery
        importlib.import_module("final_app")
        module = importlib.import_module("marketplace_listing_v5")
        enhancements = importlib.import_module("runtime_enhancements")
        delivery = importlib.import_module("delivery_history_v2")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        module._LISTING_CACHE.clear()

    def test_extracts_listing_url_from_caption_body_not_only_exact_field(self):
        event = {
            "event_id": "$caption",
            "content": {
                "msgtype": "m.image",
                "url": "mxc://matrix.example.com/chair",
                "body": "Silla de escritorio\nhttps://www.facebook.com/marketplace/item/987654321/?ref=messenger_banner",
                "com.beeper.meta.full_post_title": "Silla de escritorio",
            },
        }
        candidate = module._event_listing_candidate(event, "Silla de escritorio")
        self.assertEqual(candidate["url"], "https://www.facebook.com/marketplace/item/987654321/")
        self.assertEqual(candidate["thumbnail_mxc"], "mxc://matrix.example.com/chair")

    def test_extracts_listing_url_from_formatted_html_href(self):
        event = {
            "event_id": "$html",
            "content": {
                "msgtype": "m.text",
                "body": "Silla de escritorio",
                "formatted_body": '<a href="https://www.facebook.com/marketplace/item/987654321/?ref=messenger">Abrir</a>',
            },
        }
        candidate = module._event_listing_candidate(event, "Silla de escritorio")
        self.assertEqual(candidate["url"], "https://www.facebook.com/marketplace/item/987654321/")

    def test_unwraps_facebook_l_php_redirect_to_marketplace_item(self):
        wrapped = (
            "https://l.facebook.com/l.php?u="
            "https%3A%2F%2Fwww.facebook.com%2Fmarketplace%2Fitem%2F987654321%2F%3Fref%3Dmessenger_banner"
            "&h=opaque"
        )
        self.assertEqual(
            module._canonical_marketplace_url(wrapped),
            "https://www.facebook.com/marketplace/item/987654321/",
        )

    def test_fb_native_marketplace_uri_is_canonicalized(self):
        self.assertEqual(
            module._canonical_marketplace_url("fb://marketplace/item/987654321"),
            "https://www.facebook.com/marketplace/item/987654321/",
        )

    def test_empty_discovery_cache_retries_quickly_instead_of_six_hours(self):
        empty = Mock()
        empty.json.return_value = {"chunk": [], "end": ""}
        found = Mock()
        found.json.return_value = {
            "chunk": [{
                "event_id": "$later",
                "content": {"body": "https://facebook.com/marketplace/item/987654321/"},
            }],
            "end": "",
        }
        with patch.object(enhancements, "_matrix_get", side_effect=[empty, found]) as matrix_get, \
             patch.object(delivery, "history_days", return_value=365), \
             patch.object(module.time, "monotonic", side_effect=[100.0, 100.0, 161.0, 161.0]):
            self.assertEqual(module.discover_marketplace_listing("!market:matrix.example.com"), {})
            candidate = module.discover_marketplace_listing("!market:matrix.example.com")
        self.assertEqual(matrix_get.call_count, 2)
        self.assertEqual(candidate["url"], "https://www.facebook.com/marketplace/item/987654321/")


if __name__ == "__main__":
    unittest.main()
