import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import requests
import yaml


ROOM_ID = "!runtime-contract:matrix.example.com"
CUSTOMER_MXID = "@meta_222:matrix.example.com"
EVENT_ID = "$runtime-contract-event"


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode() if payload is not None else b""
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class ProductionRuntimeCompositionContractTests(unittest.TestCase):
    """Exercise the production-installed runtime, not isolated module implementations.

    This contract exists specifically to catch cross-module wiring regressions such as
    binding_generations_v12 calling a helper on the wrong module. Internal routing and
    persistence are real; only Matrix profile I/O and Chatwoot HTTP are replaced at
    the external network boundary.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_SERVER_NAME"] = "matrix.example.com"
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-runtime-contract"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ["MAUTRIX_REGISTRATION_PATH"] = os.path.join(cls.tmp.name, "registration.yaml")
        os.environ["MAUTRIX_META_DB_PATH"] = os.path.join(cls.tmp.name, "mautrix-meta.db")

        with open(os.environ["MAUTRIX_REGISTRATION_PATH"], "w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {
                    "id": "meta",
                    "sender_localpart": "metabot",
                    "namespaces": {"users": [], "aliases": [], "rooms": []},
                },
                fh,
            )

        with sqlite3.connect(os.environ["MAUTRIX_META_DB_PATH"]) as conn:
            conn.executescript(
                """
                CREATE TABLE user_login (bridge_id TEXT, user_mxid TEXT, id TEXT);
                CREATE TABLE portal (
                    bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT,
                    parent_id TEXT, name TEXT, metadata TEXT
                );
                CREATE TABLE message (
                    bridge_id TEXT, id TEXT, part_id TEXT, mxid TEXT,
                    room_id TEXT, room_receiver TEXT, sender_id TEXT,
                    sender_mxid TEXT, timestamp INTEGER
                );
                """
            )
            conn.execute(
                "INSERT INTO user_login VALUES(?,?,?)",
                ("meta", os.environ["MATRIX_ADMIN_MXID"], "111"),
            )
            conn.execute(
                "INSERT INTO portal VALUES(?,?,?,?,?,?,?)",
                (
                    "meta",
                    "thread-1",
                    "111",
                    ROOM_ID,
                    "",
                    "Customer conversation",
                    json.dumps({"thread_type": 1}),
                ),
            )
            conn.execute(
                "INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "meta",
                    "message-1",
                    "",
                    EVENT_ID,
                    "thread-1",
                    "111",
                    "222",
                    CUSTOMER_MXID,
                    2000,
                ),
            )

        # Import the exact production composition entrypoint. This installs every
        # runtime wrapper in production order, including binding_generations_v12 last.
        importlib.import_module("runtime_entrypoint")

        global runtime, legacy, prod, media, enhancements, bindings
        runtime = importlib.import_module("final_app")
        legacy = runtime.legacy
        prod = runtime.prod
        media = importlib.import_module("media_context_v3")
        enhancements = importlib.import_module("runtime_enhancements")
        bindings = importlib.import_module("binding_generations_v12")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            for table in (
                "event_deliveries",
                "conversation_bindings",
                "external_identities",
                "chatwoot_bindings",
                "integrations",
                "legacy_room_link_archive",
                "conversation_deletions",
                "room_links",
                "processed_events",
                "settings",
            ):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass

        legacy.set_setting("chatwoot_base_url", "http://chatwoot.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_inbox_identifier", "api-runtime-contract")
        legacy.set_setting("chatwoot_inbox_channel_type", "Channel::Api")
        legacy.set_setting("chatwoot_api_token", "token")
        legacy.set_setting("chatwoot_enabled_at_ms", "1")
        legacy.set_setting("sync_contact_profiles", "1")
        prod._portal_cache.add(ROOM_ID)

    def test_production_entrypoint_installs_expected_cross_module_contracts(self):
        self.assertTrue(bindings._INSTALLED)
        self.assertIs(prod.ensure_room_link, bindings.ensure_room_link)
        self.assertIs(legacy.ensure_room_link, bindings.ensure_room_link)
        self.assertIs(media.mirror_matrix_event, bindings.mirror_matrix_event)
        self.assertIs(media._post_chatwoot_text, bindings.post_text)
        self.assertIs(media.sync_conversation_context, bindings.sync_conversation_context)
        self.assertIs(enhancements.import_recent_history, bindings.import_recent_history)
        self.assertTrue(callable(enhancements.contact_identity))

    def test_real_production_composition_creates_projection_and_delivers_message(self):
        request_calls = []
        post_calls = []

        def fake_request(method, url, **kwargs):
            request_calls.append((method, url, kwargs))
            if method == "POST" and url.endswith("/contacts"):
                self.assertEqual(kwargs["json"]["name"], "Customer Name")
                return FakeResponse({"id": 5, "contact_inboxes": []})
            if method == "POST" and url.endswith("/contacts/5/contact_inboxes"):
                return FakeResponse({"source_id": "source-5"})
            if method == "PUT" and url.endswith("/contacts/5"):
                self.assertEqual(kwargs["json"]["name"], "Customer Name")
                return FakeResponse({})
            if method == "POST" and url.endswith("/conversations"):
                return FakeResponse({"id": 77, "display_id": 12})
            if method == "GET" and url.endswith("/conversations/77/messages"):
                return FakeResponse({"payload": []})
            if method == "POST" and url.endswith("/conversations/77/custom_attributes"):
                return FakeResponse({})
            if method == "POST" and url.endswith("/conversations/77/labels"):
                return FakeResponse({})
            self.fail(f"unexpected requests.request call: {method} {url}")

        def fake_get(url, **kwargs):
            if url.endswith("/custom_attribute_definitions"):
                # The production runtime performs fail-soft Marketplace schema
                # reconciliation. Model an already-converged Chatwoot schema so
                # the contract tests production composition without emitting
                # unrelated network-error noise.
                return FakeResponse({"payload": [
                    {"id": 1, "attribute_key": "marketplace_listing_title"},
                    {"id": 2, "attribute_key": "facebook_profile_url"},
                    {"id": 3, "attribute_key": "marketplace_listing_url"},
                ]})
            if url.endswith("/labels"):
                return FakeResponse({"payload": [
                    {
                        "id": 10,
                        "title": "marketplace",
                        "description": "Facebook Marketplace conversation",
                    }
                ]})
            if url.endswith("/conversations/77"):
                return FakeResponse(
                    {
                        "id": 77,
                        "inbox_id": 2,
                        "custom_attributes": {},
                    }
                )
            if url.endswith("/conversations/77/labels"):
                return FakeResponse({"payload": []})
            self.fail(f"unexpected requests.get call: {url}")

        def fake_post(url, **kwargs):
            post_calls.append((url, kwargs))
            if url.endswith("/conversations/77/messages"):
                return FakeResponse({"id": 9001})
            self.fail(f"unexpected requests.post call: {url}")

        event = {
            "event_id": EVENT_ID,
            "type": "m.room.message",
            "sender": CUSTOMER_MXID,
            "origin_server_ts": 2000,
            "content": {"msgtype": "m.text", "body": "hello from Meta"},
        }

        with patch.object(
            enhancements,
            "matrix_profile",
            return_value={"displayname": "Customer Name", "avatar_url": ""},
        ), patch("requests.request", side_effect=fake_request), patch(
            "requests.get", side_effect=fake_get
        ), patch("requests.post", side_effect=fake_post):
            delivered = media.mirror_matrix_event(ROOM_ID, event, history=True)

        self.assertTrue(delivered)
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0][1]["json"]["content"], "hello from Meta")
        self.assertEqual(
            post_calls[0][1]["json"]["content_attributes"]["matrix_event_id"],
            EVENT_ID,
        )

        with legacy.db() as conn:
            projection = conn.execute(
                "SELECT cb.chatwoot_conversation_id,cb.status,b.generation,b.status,"
                "cb.profile_sync_version,cb.profile_synced_at,cb.profile_sync_error "
                "FROM conversation_bindings cb "
                "JOIN chatwoot_bindings b ON b.id=cb.chatwoot_binding_id "
                "WHERE cb.matrix_room_id=?",
                (ROOM_ID,),
            ).fetchone()
            room_link = conn.execute(
                "SELECT conversation_id FROM room_links WHERE room_id=?",
                (ROOM_ID,),
            ).fetchone()
            delivery = conn.execute(
                "SELECT status,direction FROM event_deliveries WHERE event_id=?",
                (EVENT_ID,),
            ).fetchone()

        self.assertIsNotNone(projection)
        self.assertEqual(int(projection[0]), 77)
        self.assertEqual(str(projection[1]), "ACTIVE")
        self.assertEqual(int(projection[2]), 1)
        self.assertEqual(str(projection[3]), "ACTIVE")
        self.assertEqual(int(projection[4]), bindings.PROFILE_SYNC_VERSION)
        self.assertGreater(int(projection[5]), 0)
        self.assertEqual(str(projection[6]), "")
        self.assertEqual(int(room_link[0]), 77)
        self.assertEqual(str(delivery[0]), "DELIVERED")
        self.assertEqual(str(delivery[1]), "matrix_to_chatwoot")

        created_paths = [url for method, url, _ in request_calls if method == "POST"]
        self.assertTrue(any(path.endswith("/contacts") for path in created_paths))
        self.assertTrue(any(path.endswith("/conversations") for path in created_paths))
        self.assertTrue(
            any(method == "PUT" and url.endswith("/contacts/5") for method, url, _ in request_calls)
        )


if __name__ == "__main__":
    unittest.main()
