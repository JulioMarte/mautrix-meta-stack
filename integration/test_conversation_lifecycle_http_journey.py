import importlib
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import requests


class RecordingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.requests = []
        self.delete_statuses = []
        self.matrix_event_id = "$journey-delete"

    @property
    def base_url(self):
        host, port = self.server_address
        return f"http://{host}:{port}"


class MatrixHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        payload = json.loads(body.decode("utf-8"))
        self.server.requests.append({
            "method": "PUT",
            "path": self.path,
            "payload": payload,
            "authorization": self.headers.get("Authorization"),
        })
        response = {"event_id": self.server.matrix_event_id} if self.server.matrix_event_id else {}
        data = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ChatwootHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_DELETE(self):
        status = self.server.delete_statuses.pop(0) if self.server.delete_statuses else 204
        self.server.requests.append({
            "method": "DELETE",
            "path": self.path,
            "token": self.headers.get("api_access_token"),
            "status": status,
        })
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class LegacyStub:
    MATRIX_ADMIN_MXID = "@admin:matrix.example.com"

    def __init__(self, db_path, matrix_url, chatwoot_url):
        self.db_path = db_path
        self.MATRIX_HOMESERVER = matrix_url
        self._chatwoot_url = chatwoot_url
        self.settings = {
            "chatwoot_account_id": "1",
            "chatwoot_inbox_id": "2",
            "matrix_next_batch": "s1",
        }

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = str(value)

    def bridge_bot_mxid(self):
        return "@metabot:matrix.example.com"

    def chatwoot_url(self, path):
        return self._chatwoot_url + path

    def chatwoot_headers(self):
        return {"api_access_token": "journey-token"}

    def matrix_headers(self):
        return {"Authorization": "Bearer journey-matrix-token"}


class ConversationDeletionHTTPJourneyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matrix = RecordingHTTPServer(MatrixHandler)
        cls.chatwoot = RecordingHTTPServer(ChatwootHandler)
        cls.threads = [
            threading.Thread(target=cls.matrix.serve_forever, daemon=True),
            threading.Thread(target=cls.chatwoot.serve_forever, daemon=True),
        ]
        for thread in cls.threads:
            thread.start()

        cls.tmp = tempfile.TemporaryDirectory()
        cls.legacy = LegacyStub(
            str(Path(cls.tmp.name) / "integration.db"),
            cls.matrix.base_url,
            cls.chatwoot.base_url,
        )
        with cls.legacy.db() as conn:
            conn.executescript(
                """
                CREATE TABLE room_links (
                  room_id TEXT PRIMARY KEY,
                  contact_id INTEGER NOT NULL,
                  source_id TEXT NOT NULL,
                  conversation_id INTEGER NOT NULL UNIQUE,
                  created_at INTEGER NOT NULL
                );
                """
            )

        final_app = types.ModuleType("final_app")
        final_app.legacy = cls.legacy
        final_app.prod = types.SimpleNamespace(ensure_room_link=lambda room_id, sender: None)

        enhancements = types.ModuleType("runtime_enhancements")
        enhancements.handle_chatwoot_outgoing = Mock(return_value={"ok": True})
        enhancements.repair_deleted_conversation = Mock(return_value=False)
        enhancements.auto_join_room = Mock(return_value=False)
        enhancements.import_recent_history = Mock(return_value=0)
        enhancements.enhanced_live_matrix_event = Mock()

        reconcile = types.ModuleType("meta_portal_reconcile")
        reconcile.verified_meta_portal = Mock(return_value=(True, "trusted"))

        cls.saved = {name: sys.modules.get(name) for name in (
            "final_app", "runtime_enhancements", "meta_portal_reconcile", "conversation_lifecycle_v10"
        )}
        sys.modules["final_app"] = final_app
        sys.modules["runtime_enhancements"] = enhancements
        sys.modules["meta_portal_reconcile"] = reconcile
        sys.modules.pop("conversation_lifecycle_v10", None)
        cls.module = importlib.import_module("conversation_lifecycle_v10")
        cls.module.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.matrix, cls.chatwoot):
            server.shutdown()
            server.server_close()
        for name, previous in cls.saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        cls.tmp.cleanup()

    def setUp(self):
        self.matrix.requests.clear()
        self.matrix.matrix_event_id = "$journey-delete"
        self.chatwoot.requests.clear()
        self.chatwoot.delete_statuses.clear()
        with self.legacy.db() as conn:
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM verified_meta_portals")
            conn.execute("DELETE FROM conversation_deletions")

    def seed(self, conversation=77, room="!journey:matrix.example.com"):
        with self.legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id,contact_id,source_id,conversation_id,created_at) VALUES(?,?,?,?,1)",
                (room, 3, "source", conversation),
            )
        self.module.remember_verified_portal(room)
        return room

    def trusted_leave(self):
        return {
            "timeline": {
                "events": [{
                    "type": "m.room.member",
                    "state_key": self.legacy.MATRIX_ADMIN_MXID,
                    "sender": self.legacy.bridge_bot_mxid(),
                    "content": {"membership": "leave"},
                }]
            }
        }

    def payload(self, conversation=77):
        return {
            "event": "conversation_deleted",
            "conversation_id": conversation,
            "account": {"id": 1},
            "inbox": {"id": 2},
        }

    def test_chatwoot_to_meta_journey_uses_real_http_and_waits_for_bridge_confirmation(self):
        room = self.seed()

        result = self.module.process_chatwoot_delete(self.payload())
        self.assertTrue(result["remote_requested"])
        self.assertEqual(len(self.matrix.requests), 1)
        sent = self.matrix.requests[0]
        self.assertIn("/send/com.beeper.delete_chat/", sent["path"])
        self.assertEqual(
            sent["payload"],
            {"delete_for_everyone": False, "from_message_request": False},
        )
        self.assertEqual(sent["authorization"], "Bearer journey-matrix-token")
        self.assertIsNotNone(self.module._link_by_room(room))
        self.assertEqual(self.module._operation(77)["state"], "remote_requested")

        duplicate = self.module.process_chatwoot_delete(self.payload())
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(self.matrix.requests), 1, "duplicate callback must not emit another destructive event")

        confirmed = self.module.process_matrix_leave(room, self.trusted_leave())
        self.assertEqual(confirmed["origin"], "chatwoot")
        self.assertEqual(len(self.chatwoot.requests), 0, "bridge confirmation must not delete Chatwoot twice")
        self.assertIsNone(self.module._link_by_room(room))
        self.assertEqual(self.module._operation(77)["state"], "completed")

    def test_meta_to_chatwoot_journey_retries_over_http_and_suppresses_callback_loop(self):
        room = self.seed()
        self.chatwoot.delete_statuses[:] = [503, 404]

        with self.assertRaises(requests.HTTPError):
            self.module.process_matrix_leave(room, self.trusted_leave())

        failed = self.module._operation(77)
        self.assertEqual(failed["origin"], "meta")
        self.assertEqual(failed["state"], "failed_retryable")
        self.assertEqual(failed["attempts"], 1)
        self.assertIsNotNone(self.module._link_by_room(room))
        self.assertEqual(self.chatwoot.requests[0]["status"], 503)
        self.assertEqual(self.chatwoot.requests[0]["token"], "journey-token")

        retry = self.module.reconcile_retryable_meta_deletions(now=int(failed["next_retry_at"]))
        self.assertEqual(retry, {"processed": 1, "completed": 1, "failed": 0})
        self.assertEqual([entry["status"] for entry in self.chatwoot.requests], [503, 404])
        self.assertIsNone(self.module._link_by_room(room))
        self.assertEqual(self.module._operation(77)["state"], "completed")

        callback = self.module.process_chatwoot_delete(self.payload())
        self.assertEqual(callback["reason"], "meta_delete_loop_suppressed")
        self.assertEqual(len(self.matrix.requests), 0, "Meta-origin cleanup callback must not bounce back to Matrix")

    def test_matrix_missing_event_id_is_durable_and_does_not_drop_mapping(self):
        room = self.seed()
        self.matrix.matrix_event_id = ""

        with self.assertRaisesRegex(RuntimeError, "Matrix accepted no event_id"):
            self.module.process_chatwoot_delete(self.payload())

        operation = self.module._operation(77)
        self.assertEqual(operation["origin"], "chatwoot")
        self.assertEqual(operation["state"], "failed_retryable")
        self.assertEqual(operation["attempts"], 1)
        self.assertIsNotNone(self.module._link_by_room(room))
        self.assertEqual(len(self.chatwoot.requests), 0)

    def test_untrusted_leave_fails_closed_before_any_remote_http_delete(self):
        room = self.seed()
        leave = self.trusted_leave()
        leave["timeline"]["events"][0]["sender"] = self.legacy.MATRIX_ADMIN_MXID

        result = self.module.process_matrix_leave(room, leave)
        self.assertEqual(result["reason"], "leave_not_from_bridge_bot")
        self.assertEqual(len(self.chatwoot.requests), 0)
        self.assertIsNotNone(self.module._link_by_room(room))


if __name__ == "__main__":
    unittest.main()
