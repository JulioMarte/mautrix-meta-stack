import importlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.content = b"{}" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class BindingGenerationsV12Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-binding-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"

        global runtime, legacy, lifecycle, bindings
        runtime = importlib.import_module("final_app")
        lifecycle = importlib.import_module("conversation_lifecycle_v10")
        bindings = importlib.import_module("binding_generations_v12")
        legacy = runtime.legacy
        Path(legacy.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        legacy.init_db()
        lifecycle.ensure_schema()
        bindings.ensure_schema()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            for table in (
                "legacy_room_link_archive",
                "event_deliveries",
                "conversation_bindings",
                "external_identities",
                "chatwoot_bindings",
                "integrations",
                "conversation_deletions",
                "room_links",
                "processed_events",
                "settings",
            ):
                conn.execute(f"DELETE FROM {table}")
        legacy.set_setting("chatwoot_base_url", "http://old.example.com")
        legacy.set_setting("chatwoot_account_id", "1")
        legacy.set_setting("chatwoot_inbox_id", "2")
        legacy.set_setting("chatwoot_api_token", "token")
        bindings._base_event_seen = legacy.event_seen
        bindings._base_mark_event = legacy.mark_event

    def _target(self, base="http://old.example.com", account=1, inbox=2, identifier="api-a"):
        return bindings.BindingTarget(base, account, inbox, identifier, "Channel::Api")

    def _seed_identity_projection(self, binding, conversation_id=77, status="ACTIVE"):
        now = int(time.time())
        with legacy.db() as conn:
            iid = bindings._integration_id(conn)
            ext = conn.execute(
                "INSERT INTO external_identities"
                "(integration_id,bridge_id,portal_id,portal_receiver,matrix_room_id,"
                "first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?)",
                (iid, "meta", "thread-1", "receiver-1", "!room:example.com", now, now),
            )
            external_id = int(ext.lastrowid)
            proj = conn.execute(
                "INSERT INTO conversation_bindings"
                "(chatwoot_binding_id,external_identity_id,matrix_room_id,chatwoot_contact_id,"
                "chatwoot_source_id,chatwoot_conversation_id,status,last_verified_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,0,?,?)",
                (
                    int(binding["id"]), external_id, "!room:example.com", 5,
                    "source-1", conversation_id, status, now, now,
                ),
            )
            return int(proj.lastrowid)

    def test_rebind_is_monotonic_and_archives_legacy_cache(self):
        first = bindings.activate_target(self._target(), "initial")
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id,contact_id,source_id,conversation_id,created_at) "
                "VALUES(?,?,?,?,?)",
                ("!room:example.com", 5, "source-1", 77, int(time.time())),
            )
            conn.execute(
                "INSERT INTO processed_events(event_id,direction,created_at) VALUES(?,?,?)",
                ("$event", "matrix_to_chatwoot", int(time.time())),
            )

        second = bindings.activate_target(
            self._target(base="http://new.example.com", account=9, inbox=8, identifier="api-b"),
            "configuration_change",
            force_new=True,
        )

        self.assertEqual(int(first["generation"]), 1)
        self.assertEqual(int(second["generation"]), 2)
        with legacy.db() as conn:
            old = conn.execute(
                "SELECT status,superseded_by FROM chatwoot_bindings WHERE id=?",
                (int(first["id"]),),
            ).fetchone()
            self.assertEqual(old["status"], "RETIRED")
            self.assertEqual(int(old["superseded_by"]), int(second["id"]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM room_links").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM legacy_room_link_archive").fetchone()[0], 1
            )
            # Audit/dedupe history is no longer destructively wiped.
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0], 1)

    def test_projection_creation_uses_runtime_contact_identity_and_persists_mapping(self):
        binding = bindings.activate_target(self._target(), "initial")
        now = int(time.time())
        with legacy.db() as conn:
            integration_id = bindings._integration_id(conn)
            cursor = conn.execute(
                "INSERT INTO external_identities"
                "(integration_id,bridge_id,remote_account_id,portal_id,portal_receiver,"
                "matrix_room_id,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    integration_id, "meta", "account-1", "thread-1", "receiver-1",
                    "!room:example.com", now, now,
                ),
            )
            external = conn.execute(
                "SELECT * FROM external_identities WHERE id=?", (int(cursor.lastrowid),)
            ).fetchone()

        identity = {
            "bridge_id": "meta",
            "remote_account_id": "account-1",
            "portal_id": "thread-1",
            "portal_receiver": "receiver-1",
            "matrix_room_id": "!room:example.com",
        }

        def fake_request(_binding, method, path, **kwargs):
            self.assertEqual(method, "POST")
            if path.endswith("/contacts"):
                self.assertEqual(kwargs["json"]["name"], "Customer Name")
                return {"id": 5}
            if path.endswith("/contact_inboxes"):
                return {"source_id": "source-1"}
            if path.endswith("/conversations"):
                return {"id": 77, "display_id": 12}
            self.fail(f"unexpected Chatwoot request: {method} {path}")

        with patch.object(
            bindings.enhancements,
            "contact_identity",
            return_value={"name": "Customer Name", "avatar_url": ""},
        ) as contact_identity, patch.object(
            bindings, "_request", side_effect=fake_request
        ), patch.object(
            bindings.prod, "contact_object", side_effect=lambda payload: payload
        ), patch.object(
            bindings.prod, "contact_source_id", return_value=""
        ), patch.object(
            bindings, "_clear_binding_dedupe_for_room", return_value=0
        ):
            projection = bindings._create_projection(
                binding, external, identity, "@meta_customer:matrix.example.com"
            )

        contact_identity.assert_called_once_with("@meta_customer:matrix.example.com")
        self.assertEqual(int(projection["chatwoot_conversation_id"]), 77)
        self.assertEqual(str(projection["status"]), "ACTIVE")
        with legacy.db() as conn:
            room_link = conn.execute(
                "SELECT * FROM room_links WHERE room_id='!room:example.com'"
            ).fetchone()
        self.assertIsNotNone(room_link)
        self.assertEqual(int(room_link["conversation_id"]), 77)

    def test_same_target_does_not_create_generation(self):
        first = bindings.activate_target(self._target(), "initial")
        second = bindings.activate_target(self._target(), "health_verified", force_new=False)
        self.assertEqual(int(first["id"]), int(second["id"]))
        self.assertEqual(int(second["generation"]), 1)

    def test_returning_to_old_target_creates_new_generation_not_rollback(self):
        first = bindings.activate_target(self._target(), "initial")
        second = bindings.activate_target(
            self._target(base="http://new.example.com", inbox=9, identifier="api-b"),
            "change",
            force_new=True,
        )
        third = bindings.activate_target(self._target(), "change_back", force_new=True)
        self.assertEqual(
            [int(first["generation"]), int(second["generation"]), int(third["generation"])],
            [1, 2, 3],
        )

    def test_event_dedupe_is_scoped_to_binding_generation(self):
        first = bindings.activate_target(self._target(), "initial")
        bindings.mark_event("$same-event", "matrix_to_chatwoot")
        self.assertTrue(bindings.event_seen("$same-event"))

        second = bindings.activate_target(
            self._target(base="http://new.example.com", inbox=9, identifier="api-b"),
            "change",
            force_new=True,
        )
        self.assertNotEqual(int(first["id"]), int(second["id"]))
        self.assertFalse(bindings.event_seen("$same-event"))
        with legacy.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM processed_events WHERE event_id='$same-event'"
                ).fetchone()[0],
                1,
            )

    def test_global_legacy_dedupe_is_not_seeded_into_fresh_generation(self):
        binding = bindings.activate_target(self._target(), "initial")
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO processed_events(event_id,direction,created_at) VALUES(?,?,?)",
                ("$old-room-event", "matrix_to_chatwoot", int(time.time())),
            )
        bindings._seed_dedupe()
        with legacy.db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM event_deliveries WHERE chatwoot_binding_id=?",
                (int(binding["id"]),),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_legacy_dedupe_migrates_only_for_adopted_room(self):
        binding = bindings.activate_target(self._target(), "initial")
        now = int(time.time())
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO processed_events(event_id,direction,created_at) VALUES(?,?,?)",
                ("$room-a", "matrix_to_chatwoot", now),
            )
            conn.execute(
                "INSERT INTO processed_events(event_id,direction,created_at) VALUES(?,?,?)",
                ("$room-b", "matrix_to_chatwoot", now),
            )
        with patch.object(bindings, "_room_event_ids", return_value=["$room-a"]):
            migrated = bindings._migrate_legacy_dedupe_for_room(
                int(binding["id"]), "!room:example.com"
            )
        self.assertEqual(migrated, 1)
        with legacy.db() as conn:
            rows = conn.execute(
                "SELECT event_id FROM event_deliveries WHERE chatwoot_binding_id=?",
                (int(binding["id"]),),
            ).fetchall()
        self.assertEqual([str(row[0]) for row in rows], ["$room-a"])

    def test_empty_projection_repair_clears_only_current_room_generation_dedupe(self):
        binding = bindings.activate_target(self._target(), "initial")
        projection_id = self._seed_identity_projection(binding, conversation_id=77)
        now = int(time.time())
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO event_deliveries"
                "(event_id,chatwoot_binding_id,direction,status,created_at,delivered_at) "
                "VALUES(?,?,?,'DELIVERED',?,?)",
                ("$room-event", int(binding["id"]), "matrix_to_chatwoot", now, now),
            )
            conn.execute(
                "INSERT INTO event_deliveries"
                "(event_id,chatwoot_binding_id,direction,status,created_at,delivered_at) "
                "VALUES(?,?,?,'DELIVERED',?,?)",
                ("$other-event", int(binding["id"]), "matrix_to_chatwoot", now, now),
            )
        with patch.object(bindings, "_room_event_ids", return_value=["$room-event"]), \
             patch.object(
                 bindings,
                 "_request",
                 return_value={"payload": [{"message_type": 2, "content": "label activity"}]},
             ):
            removed = bindings._repair_empty_projection_dedupe("!room:example.com")
        self.assertEqual(removed, 1)
        with legacy.db() as conn:
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM event_deliveries WHERE event_id='$room-event'"
                ).fetchone()
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM event_deliveries WHERE event_id='$other-event'"
                ).fetchone()
            )
            checked = conn.execute(
                "SELECT history_repair_checked_at FROM conversation_bindings WHERE id=?",
                (projection_id,),
            ).fetchone()[0]
        self.assertGreater(int(checked), 0)

    def test_nonempty_projection_keeps_generation_dedupe(self):
        binding = bindings.activate_target(self._target(), "initial")
        self._seed_identity_projection(binding, conversation_id=77)
        now = int(time.time())
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO event_deliveries"
                "(event_id,chatwoot_binding_id,direction,status,created_at,delivered_at) "
                "VALUES(?,?,?,'DELIVERED',?,?)",
                ("$room-event", int(binding["id"]), "matrix_to_chatwoot", now, now),
            )
        with patch.object(bindings, "_room_event_ids", return_value=["$room-event"]), \
             patch.object(
                 bindings,
                 "_request",
                 return_value={"payload": [{"message_type": 0, "content": "real customer message"}]},
             ):
            removed = bindings._repair_empty_projection_dedupe("!room:example.com")
        self.assertEqual(removed, 0)
        with legacy.db() as conn:
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM event_deliveries WHERE event_id='$room-event'"
                ).fetchone()
            )


    def test_404_without_tombstone_marks_projection_stale(self):
        binding = bindings.activate_target(self._target(), "initial")
        projection_id = self._seed_identity_projection(binding)
        with legacy.db() as conn:
            projection = conn.execute(
                "SELECT * FROM conversation_bindings WHERE id=?", (projection_id,)
            ).fetchone()

        error = requests.HTTPError("404")
        error.response = FakeResponse(status=404)
        with patch.object(bindings, "_request", side_effect=error):
            self.assertFalse(bindings._verify_projection(binding, projection, force=True))

        with legacy.db() as conn:
            row = conn.execute(
                "SELECT status,stale_reason FROM conversation_bindings WHERE id=?",
                (projection_id,),
            ).fetchone()
        self.assertEqual(row["status"], "STALE")
        self.assertEqual(row["stale_reason"], "chatwoot_404")

    def test_404_with_deletion_tombstone_never_recreates(self):
        binding = bindings.activate_target(self._target(), "initial")
        projection_id = self._seed_identity_projection(binding)
        lifecycle._start_operation(77, "!room:example.com", "meta", "completed")
        with legacy.db() as conn:
            projection = conn.execute(
                "SELECT * FROM conversation_bindings WHERE id=?", (projection_id,)
            ).fetchone()

        error = requests.HTTPError("404")
        error.response = FakeResponse(status=404)
        with patch.object(bindings, "_request", side_effect=error):
            with self.assertRaises(bindings.IntentionalConversationDeletion):
                bindings._verify_projection(binding, projection, force=True)

        with legacy.db() as conn:
            row = conn.execute(
                "SELECT status,stale_reason FROM conversation_bindings WHERE id=?",
                (projection_id,),
            ).fetchone()
        self.assertEqual(row["status"], "DELETED")
        self.assertEqual(row["stale_reason"], "intentional_delete")

    def test_old_generation_delete_uses_original_target(self):
        old = bindings.activate_target(self._target(), "initial")
        projection_id = self._seed_identity_projection(old)
        lifecycle._start_operation(77, "!room:example.com", "meta", "completed")
        with legacy.db() as conn:
            conn.execute(
                "UPDATE conversation_deletions SET binding_id=?,conversation_binding_id=?,"
                "binding_generation=1 WHERE conversation_id=77",
                (int(old["id"]), projection_id),
            )

        bindings.activate_target(
            self._target(base="http://new.example.com", account=9, inbox=8, identifier="api-b"),
            "change",
            force_new=True,
        )
        response = Mock(status_code=404)
        response.raise_for_status = Mock()
        with patch.object(bindings.requests, "delete", return_value=response) as delete:
            bindings.lifecycle_delete_chatwoot(77)

        url = delete.call_args.args[0]
        self.assertEqual(
            url, "http://old.example.com/api/v1/accounts/1/conversations/77"
        )

    def test_config_is_remote_validated_before_persisting(self):
        bindings._base_save_configuration = Mock()
        target = self._target(base="http://validated.example.com", account=5, inbox=6)
        with patch.object(bindings, "validate_target", return_value=target):
            bindings.save_configuration(
                "http://validated.example.com", "5", "6", "new-token", False, ""
            )
        bindings._base_save_configuration.assert_called_once()

        bindings._base_save_configuration.reset_mock()
        with patch.object(bindings, "validate_target", side_effect=RuntimeError("bad inbox")):
            with self.assertRaisesRegex(RuntimeError, "bad inbox"):
                bindings.save_configuration(
                    "http://bad.example.com", "5", "999", "new-token", False, ""
                )
        bindings._base_save_configuration.assert_not_called()


if __name__ == "__main__":
    unittest.main()
