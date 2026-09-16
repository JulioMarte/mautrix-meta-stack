import importlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


class LegacyStub:
    MATRIX_ADMIN_MXID = "@admin:matrix.example.com"

    def __init__(self):
        self.seen = set()

    def event_seen(self, event_id):
        return event_id in self.seen


class StalePortalMaterializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.legacy = LegacyStub()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = cls.tmp.name + "/meta.db"
        with sqlite3.connect(cls.db_path) as conn:
            conn.executescript("""
                CREATE TABLE user_login(user_mxid TEXT, id TEXT);
                CREATE TABLE portal(bridge_id TEXT, id TEXT, receiver TEXT, mxid TEXT);
                CREATE TABLE message(
                    bridge_id TEXT, room_id TEXT, room_receiver TEXT,
                    sender_id TEXT, mxid TEXT, timestamp INTEGER
                );
            """)
            conn.execute("INSERT INTO user_login VALUES(?, ?)", (cls.legacy.MATRIX_ADMIN_MXID, "self"))
            conn.execute("INSERT INTO portal VALUES(?, ?, ?, ?)", ("meta", "thread", "self", "!room:matrix.example.com"))
            conn.execute(
                "INSERT INTO message VALUES(?, ?, ?, ?, ?, ?)",
                ("meta", "thread", "self", "self", "$self-newer", 300),
            )
            conn.execute(
                "INSERT INTO message VALUES(?, ?, ?, ?, ?, ?)",
                ("meta", "thread", "self", "customer", "$customer-old", 200),
            )

        runtime = types.ModuleType("final_app")
        runtime.legacy = cls.legacy

        media = types.ModuleType("media_context_v3")
        media._meta_db = lambda: sqlite3.connect(cls.db_path)
        media._self_remote_ids = lambda conn: {
            str(row[0]) for row in conn.execute(
                "SELECT id FROM user_login WHERE user_mxid = ?", (cls.legacy.MATRIX_ADMIN_MXID,)
            ).fetchall()
        }
        media.message_direction = lambda event: event.get("direction", "incoming")
        media.mirror_matrix_event = Mock(return_value=True)

        enhancements = types.ModuleType("runtime_enhancements")
        enhancements.import_recent_history = Mock(return_value=0)
        enhancements.existing_room_link = Mock(return_value=None)
        enhancements._matrix_get = Mock()

        cls.saved_modules = {
            name: sys.modules.get(name)
            for name in ("final_app", "media_context_v3", "runtime_enhancements", "stale_portal_materialization_v8")
        }
        sys.modules["final_app"] = runtime
        sys.modules["media_context_v3"] = media
        sys.modules["runtime_enhancements"] = enhancements
        sys.modules.pop("stale_portal_materialization_v8", None)
        cls.module = importlib.import_module("stale_portal_materialization_v8")
        cls.media = media
        cls.enhancements = enhancements

    @classmethod
    def tearDownClass(cls):
        for name, previous in cls.saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        cls.tmp.cleanup()

    def setUp(self):
        self.legacy.seen.clear()
        self.module._base_import_history = Mock(return_value=0)
        self.enhancements.existing_room_link = Mock(return_value=None)
        self.media.mirror_matrix_event.reset_mock(return_value=True)
        self.media.mirror_matrix_event.return_value = True

    def test_latest_customer_event_excludes_logged_in_meta_account(self):
        self.assertEqual(
            self.module.latest_customer_event_id("!room:matrix.example.com"),
            "$customer-old",
        )

    def test_old_customer_anchor_materializes_unlinked_portal(self):
        event = {
            "event_id": "$customer-old",
            "type": "m.room.message",
            "direction": "incoming",
            "content": {"msgtype": "m.text", "body": "older customer message"},
        }
        with patch.object(self.module, "fetch_matrix_event", return_value=event):
            imported = self.module.import_recent_history("!room:matrix.example.com")
        self.assertEqual(imported, 1)
        self.media.mirror_matrix_event.assert_called_once_with(
            "!room:matrix.example.com", event, history=True
        )

    def test_existing_link_never_uses_stale_anchor(self):
        self.module._base_import_history = Mock(return_value=4)
        self.enhancements.existing_room_link = Mock(return_value={"conversation_id": 77})
        with patch.object(self.module, "latest_customer_event_id") as latest:
            imported = self.module.import_recent_history("!room:matrix.example.com")
        self.assertEqual(imported, 4)
        latest.assert_not_called()

    def test_outgoing_anchor_is_rejected(self):
        event = {
            "event_id": "$customer-old",
            "type": "m.room.message",
            "direction": "outgoing",
            "content": {"msgtype": "m.text", "body": "self"},
        }
        with patch.object(self.module, "fetch_matrix_event", return_value=event):
            imported = self.module.import_recent_history("!room:matrix.example.com")
        self.assertEqual(imported, 0)
        self.media.mirror_matrix_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
