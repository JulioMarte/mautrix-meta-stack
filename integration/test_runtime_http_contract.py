import importlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml


ROOM_ID = "!runtime-http:matrix.example.com"
CUSTOMER_MXID = "@meta_333:matrix.example.com"
EVENT_ID = "$runtime-http-event"


class ChatwootState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.contacts = []
        self.conversations = {}
        self.messages = []
        self.labels = {}
        self.requests = []


STATE = ChatwootState()


class ChatwootHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode()) if raw else {}

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, body=None):
        STATE.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "token": self.headers.get("api_access_token"),
                "body": body,
            }
        )

    def do_GET(self):
        self._record()
        if self.path == "/api/v1/accounts/1/conversations/77":
            conversation = STATE.conversations.get(77)
            if not conversation:
                return self._send(404, {"error": "not found"})
            return self._send(200, conversation)
        if self.path == "/api/v1/accounts/1/conversations/77/messages":
            return self._send(200, {"payload": list(STATE.messages)})
        if self.path == "/api/v1/accounts/1/conversations/77/labels":
            return self._send(200, {"payload": STATE.labels.get(77, [])})
        return self._send(404, {"error": f"unexpected GET {self.path}"})

    def do_POST(self):
        body = self._json_body()
        self._record(body)
        if self.path == "/api/v1/accounts/1/contacts":
            contact = {
                "id": 5,
                "name": body.get("name"),
                "identifier": body.get("identifier"),
                "contact_inboxes": [],
            }
            STATE.contacts.append(contact)
            return self._send(200, contact)
        if self.path == "/api/v1/accounts/1/contacts/5/contact_inboxes":
            return self._send(200, {"source_id": "source-5"})
        if self.path == "/api/v1/accounts/1/conversations":
            conversation = {
                "id": 77,
                "display_id": 12,
                "inbox_id": body.get("inbox_id"),
                "custom_attributes": body.get("custom_attributes") or {},
            }
            STATE.conversations[77] = conversation
            return self._send(200, conversation)
        if self.path == "/api/v1/accounts/1/conversations/77/custom_attributes":
            conversation = STATE.conversations[77]
            conversation["custom_attributes"] = body.get("custom_attributes") or {}
            return self._send(200, conversation)
        if self.path == "/api/v1/accounts/1/conversations/77/labels":
            STATE.labels[77] = list(body.get("labels") or [])
            return self._send(200, {"payload": STATE.labels[77]})
        if self.path == "/api/v1/accounts/1/conversations/77/messages":
            message = {
                "id": 9000 + len(STATE.messages) + 1,
                "content": body.get("content"),
                "message_type": body.get("message_type"),
                "content_attributes": body.get("content_attributes") or {},
            }
            STATE.messages.append(message)
            return self._send(200, message)
        return self._send(404, {"error": f"unexpected POST {self.path}"})


class ProductionRuntimeHttpContractTests(unittest.TestCase):
    """Cross the real production runtime and a real HTTP socket boundary.

    No requests monkey-patching is used. The only fake is the remote Chatwoot
    service itself, which behaves as a protocol test double and records what the
    production client actually sends over HTTP.
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
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-runtime-http-contract"
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
                    "thread-http-1",
                    "111",
                    ROOM_ID,
                    "",
                    "HTTP contract conversation",
                    json.dumps({"thread_type": 1}),
                ),
            )
            conn.execute(
                "INSERT INTO message VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "meta",
                    "message-http-1",
                    "",
                    EVENT_ID,
                    "thread-http-1",
                    "111",
                    "333",
                    CUSTOMER_MXID,
                    3000,
                ),
            )

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ChatwootHandler)
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

        importlib.import_module("runtime_entrypoint")
        global runtime, legacy, prod, media
        runtime = importlib.import_module("final_app")
        legacy = runtime.legacy
        prod = runtime.prod
        media = importlib.import_module("media_context_v3")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.http_thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        STATE.reset()
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

        legacy.set_setting("chatwoot_base_url", self.base_url)
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_inbox_identifier", "api-runtime-http")
        legacy.set_setting("chatwoot_inbox_channel_type", "Channel::Api")
        legacy.set_setting("chatwoot_api_token", "http-contract-token")
        legacy.set_setting("chatwoot_enabled_at_ms", "1")
        legacy.set_setting("sync_contact_profiles", "0")
        prod._portal_cache.add(ROOM_ID)

    def test_real_http_boundary_creates_projection_delivers_and_deduplicates(self):
        event = {
            "event_id": EVENT_ID,
            "type": "m.room.message",
            "sender": CUSTOMER_MXID,
            "origin_server_ts": 3000,
            "content": {"msgtype": "m.text", "body": "real HTTP message"},
        }

        self.assertTrue(media.mirror_matrix_event(ROOM_ID, event, history=True))
        self.assertFalse(media.mirror_matrix_event(ROOM_ID, event, history=True))

        self.assertEqual(len(STATE.contacts), 1)
        self.assertEqual(len(STATE.conversations), 1)
        self.assertEqual(len(STATE.messages), 1)
        self.assertEqual(STATE.messages[0]["content"], "real HTTP message")
        self.assertEqual(
            STATE.messages[0]["content_attributes"]["matrix_event_id"],
            EVENT_ID,
        )

        mutating = [item for item in STATE.requests if item["method"] == "POST"]
        self.assertTrue(all(item["token"] == "http-contract-token" for item in mutating))
        self.assertEqual(
            sum(item["path"].endswith("/messages") for item in mutating),
            1,
        )

        with legacy.db() as conn:
            projection = conn.execute(
                "SELECT cb.chatwoot_conversation_id,cb.status,b.status "
                "FROM conversation_bindings cb "
                "JOIN chatwoot_bindings b ON b.id=cb.chatwoot_binding_id "
                "WHERE cb.matrix_room_id=?",
                (ROOM_ID,),
            ).fetchone()
            delivery = conn.execute(
                "SELECT status,direction FROM event_deliveries WHERE event_id=?",
                (EVENT_ID,),
            ).fetchone()

        self.assertEqual(int(projection[0]), 77)
        self.assertEqual(str(projection[1]), "ACTIVE")
        self.assertEqual(str(projection[2]), "ACTIVE")
        self.assertEqual(str(delivery[0]), "DELIVERED")
        self.assertEqual(str(delivery[1]), "matrix_to_chatwoot")


if __name__ == "__main__":
    unittest.main()
