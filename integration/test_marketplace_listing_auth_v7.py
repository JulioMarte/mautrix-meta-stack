import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import yaml


class MarketplaceListingAuthV7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-v7-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        os.environ["MAUTRIX_META_DB_PATH"] = os.path.join(cls.tmp.name, "mautrix-meta.db")
        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {"id": "meta", "sender_localpart": "metabot", "namespaces": {"users": [], "aliases": [], "rooms": []}},
                fh,
            )
        login_metadata = json.dumps({
            "platform": "facebook",
            "cookies": {"c_user": "111", "xs": "session", "datr": "device"},
            "login_ua": "Mozilla/5.0 Test",
        })
        with sqlite3.connect(os.environ["MAUTRIX_META_DB_PATH"]) as conn:
            conn.executescript("""
                CREATE TABLE user_login (bridge_id TEXT, user_mxid TEXT, id TEXT, metadata TEXT);
                CREATE TABLE portal (bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT, parent_id TEXT, name TEXT, metadata TEXT);
                CREATE TABLE message (bridge_id TEXT, id TEXT, part_id TEXT, mxid TEXT, room_id TEXT, room_receiver TEXT, sender_id TEXT, sender_mxid TEXT, timestamp INTEGER);
            """)
            conn.execute(
                "INSERT INTO user_login VALUES(?,?,?,?)",
                ("meta", os.environ["MATRIX_ADMIN_MXID"], "111", login_metadata),
            )
            conn.execute(
                "INSERT INTO portal VALUES(?,?,?,?,?,?,?)",
                ("meta", "4353439204919012", "111", "!market:matrix.example.com", "-12", "Alberto · Silla de escritorio", json.dumps({"thread_type": 5})),
            )

        importlib.import_module("final_app")
        global module
        module = importlib.import_module("marketplace_listing_auth_v7")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        module._PAGE_CACHE.clear()

    def test_extracts_explicit_marketplace_item_url(self):
        page = '<a href="https://www.facebook.com/marketplace/item/123456789012345/">Silla</a>'
        self.assertEqual(
            module._extract_marketplace_item_url(page, "Silla"),
            "https://www.facebook.com/marketplace/item/123456789012345/",
        )

    def test_extracts_json_escaped_marketplace_item_url(self):
        page = r'{"url":"https:\/\/www.facebook.com\/marketplace\/item\/123456789012345\/"}'
        self.assertEqual(
            module._extract_marketplace_item_url(page),
            "https://www.facebook.com/marketplace/item/123456789012345/",
        )

    def test_extracts_url_encoded_marketplace_item_path(self):
        page = "https%3A%2F%2Fwww.facebook.com%2Fmarketplace%2Fitem%2F123456789012345%2F"
        self.assertEqual(
            module._extract_marketplace_item_url(page),
            "https://www.facebook.com/marketplace/item/123456789012345/",
        )

    def test_title_correlation_selects_item_among_recommendations(self):
        page = (
            "recommended https://www.facebook.com/marketplace/item/11111111111/ unrelated product "
            + ("x" * 4000)
            + " Silla de escritorio original conversation "
            "https://www.facebook.com/marketplace/item/22222222222/"
        )
        self.assertEqual(
            module._extract_marketplace_item_url(page, "Silla de escritorio"),
            "https://www.facebook.com/marketplace/item/22222222222/",
        )

    def test_ambiguous_multiple_item_urls_fail_closed(self):
        page = (
            "https://www.facebook.com/marketplace/item/11111111111/ "
            "https://www.facebook.com/marketplace/item/22222222222/"
        )
        self.assertEqual(module._extract_marketplace_item_url(page), "")

    def test_reads_required_session_cookies_without_exposing_other_state(self):
        cookies, user_agent = module._facebook_session_material()
        self.assertEqual(cookies["c_user"], "111")
        self.assertEqual(cookies["xs"], "session")
        self.assertEqual(cookies["datr"], "device")
        self.assertEqual(user_agent, "Mozilla/5.0 Test")

    def test_best_url_prefers_matrix_xma_item_link(self):
        item = "https://www.facebook.com/marketplace/item/77777777777/"
        with patch.object(module.listing, "discover_marketplace_listing", return_value={"url": item}), \
             patch.object(module, "discover_marketplace_item_from_facebook") as page_lookup:
            result = module._best_marketplace_url("!market:matrix.example.com", "Silla de escritorio")
        self.assertEqual(result, (item, "listing"))
        page_lookup.assert_not_called()

    def test_best_url_uses_authenticated_thread_page_before_thread_fallback(self):
        item = "https://www.facebook.com/marketplace/item/88888888888/"
        with patch.object(module.listing, "discover_marketplace_listing", return_value={}), \
             patch.object(module, "discover_marketplace_item_from_facebook", return_value=item):
            result = module._best_marketplace_url("!market:matrix.example.com", "Silla de escritorio")
        self.assertEqual(result, (item, "listing"))

    def test_best_url_falls_back_to_authoritative_thread_when_item_is_unavailable(self):
        with patch.object(module.listing, "discover_marketplace_listing", return_value={}), \
             patch.object(module, "discover_marketplace_item_from_facebook", return_value=""):
            result = module._best_marketplace_url("!market:matrix.example.com", "Silla de escritorio")
        self.assertEqual(result, ("https://www.facebook.com/messages/t/4353439204919012/", "thread"))


if __name__ == "__main__":
    unittest.main()
