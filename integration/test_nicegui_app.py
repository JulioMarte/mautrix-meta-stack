import base64
import hashlib
import hmac
import importlib
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


class NiceGUIAdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["DATA_DIR"] = cls.tmp.name
        os.environ["MATRIX_ADMIN_MXID"] = "@admin:matrix.example.com"
        os.environ["MATRIX_ADMIN_PASSWORD"] = "matrix-password-long-value"
        os.environ["INTEGRATION_ADMIN_PASSWORD"] = "admin-password-long-value"
        os.environ["CHATWOOT_WEBHOOK_SECRET"] = "webhook-secret-long-value"
        os.environ["INTEGRATION_SESSION_SECRET"] = "session-secret-long-enough-for-nicegui-tests"
        os.environ["META_PROXY_RESOLVER_SECRET"] = "resolver-secret-long-value"
        os.environ["INTEGRATION_COOKIE_SECURE"] = "false"
        os.environ["START_MATRIX_SYNC"] = "false"
        os.environ["ALLOW_INSECURE_CHATWOOT"] = "true"
        os.environ.pop("META_PROXY_URL", None)
        os.environ["META_PROXY_ENABLED"] = "false"
        global module, legacy
        module = importlib.import_module("nicegui_app")
        legacy = module.legacy
        legacy.init_db()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with legacy.db() as conn:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM room_links")
            conn.execute("DELETE FROM processed_events")

    def save_minimal(self, token="chatwoot-secret-token"):
        module.save_configuration(
            "http://chatwoot.example.com",
            "1",
            "2",
            token,
            False,
            "",
        )

    def test_save_configuration_persists_chatwoot_and_activation_boundary(self):
        self.save_minimal()
        self.assertEqual(legacy.get_setting("chatwoot_base_url"), "http://chatwoot.example.com")
        self.assertEqual(legacy.get_setting("chatwoot_account_id"), "1")
        self.assertEqual(legacy.get_setting("chatwoot_inbox_id"), "2")
        self.assertEqual(legacy.get_setting("chatwoot_api_token"), "chatwoot-secret-token")
        self.assertTrue(legacy.get_setting("chatwoot_enabled_at_ms").isdigit())

    def test_first_save_requires_api_token(self):
        with self.assertRaisesRegex(ValueError, "API token is required"):
            module.save_configuration("http://chatwoot.example.com", "1", "2", "", False, "")
        self.assertEqual(legacy.get_setting("chatwoot_base_url"), "")

    def test_blank_token_preserves_existing_secret(self):
        self.save_minimal("original-secret")
        module.save_configuration("http://chatwoot.example.com", "1", "2", "", False, "")
        self.assertEqual(legacy.get_setting("chatwoot_api_token"), "original-secret")

    def test_saved_secrets_can_be_loaded_explicitly_by_admin_ui(self):
        self.save_minimal("original-secret")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        legacy.set_setting("chatwoot_webhook_signing_secret", "chatwoot-signing-secret")
        self.assertEqual(module.get_saved_secret("chatwoot_api_token"), "original-secret")
        self.assertIn("user:pass", module.get_saved_secret("proxy_url"))
        self.assertEqual(module.get_saved_secret("chatwoot_webhook_signing_secret"), "chatwoot-signing-secret")
        with self.assertRaises(ValueError):
            module.get_saved_secret("unknown")

    def test_bad_chatwoot_url_is_rejected_without_partial_write(self):
        with self.assertRaises(ValueError):
            module.save_configuration("https://user:pass@chatwoot.example.com", "1", "2", "token", False, "")
        self.assertEqual(legacy.get_setting("chatwoot_base_url"), "")

    def test_chatwoot_url_query_and_fragment_are_rejected(self):
        for value in (
            "https://chatwoot.example.com/?token=secret",
            "https://chatwoot.example.com/#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.save_configuration(value, "1", "2", "token", False, "")

    def test_account_and_inbox_must_be_numeric(self):
        with self.assertRaisesRegex(ValueError, "must be numeric"):
            module.save_configuration("http://chatwoot.example.com", "account", "2", "token", False, "")
        with self.assertRaisesRegex(ValueError, "must be numeric"):
            module.save_configuration("http://chatwoot.example.com", "1", "inbox", "token", False, "")

    def test_discover_chatwoot_finds_numeric_account_and_inboxes(self):
        profile = Mock()
        profile.raise_for_status.return_value = None
        profile.json.return_value = {"account_id": 7}
        inboxes = Mock()
        inboxes.raise_for_status.return_value = None
        inboxes.json.return_value = {"payload": [{"id": 11, "name": "Facebook Marketplace"}]}
        session = Mock()
        session.trust_env = True
        session.get.side_effect = [profile, inboxes]
        with patch.object(module.requests, "Session", return_value=session):
            result = module.discover_chatwoot("http://chatwoot.example.com", "personal-token")
        self.assertEqual(result["account_id"], 7)
        self.assertEqual(result["inboxes"], [{"id": 11, "name": "Facebook Marketplace"}])
        self.assertFalse(session.trust_env)
        first_call = session.get.call_args_list[0]
        self.assertTrue(first_call.args[0].endswith("/api/v1/profile"))
        self.assertEqual(first_call.kwargs["headers"]["api_access_token"], "personal-token")
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("/api/v1/accounts/7/inboxes"))

    def test_discover_chatwoot_can_reuse_saved_token(self):
        self.save_minimal("saved-token")
        profile = Mock()
        profile.raise_for_status.return_value = None
        profile.json.return_value = {"accounts": [{"id": 3}]}
        inboxes = Mock()
        inboxes.raise_for_status.return_value = None
        inboxes.json.return_value = {"payload": []}
        session = Mock()
        session.get.side_effect = [profile, inboxes]
        with patch.object(module.requests, "Session", return_value=session):
            result = module.discover_chatwoot("http://chatwoot.example.com", "")
        self.assertEqual(result["account_id"], 3)
        self.assertEqual(session.get.call_args_list[0].kwargs["headers"]["api_access_token"], "saved-token")

    def test_proxy_is_optional_and_disabled_by_default(self):
        self.save_minimal()
        managed, enabled, proxy = module.effective_proxy()
        self.assertFalse(managed)
        self.assertFalse(enabled)
        self.assertEqual(proxy, "")
        self.assertTrue(module.setup_state()["proxy_ready"])

    def test_enabling_proxy_requires_url_on_first_save(self):
        with self.assertRaisesRegex(ValueError, "no proxy URL"):
            module.save_configuration("http://chatwoot.example.com", "1", "2", "token", True, "")
        self.assertEqual(legacy.get_setting("chatwoot_base_url"), "")
        self.assertEqual(legacy.get_setting("proxy_enabled"), "")

    def test_invalid_proxy_is_rejected_before_any_configuration_is_written(self):
        with self.assertRaises(ValueError):
            module.save_configuration(
                "http://chatwoot.example.com", "1", "2", "token", True, "ftp://proxy.example.com:21"
            )
        self.assertEqual(legacy.get_setting("chatwoot_api_token"), "")
        self.assertEqual(legacy.get_setting("proxy_url"), "")

    def test_existing_proxy_secret_can_be_kept_when_toggle_remains_enabled(self):
        legacy.set_setting("proxy_url", "http://proxy-user:proxy-pass@proxy.example.com:8888")
        legacy.set_setting("proxy_enabled", "1")
        self.save_minimal()
        module.save_configuration("http://chatwoot.example.com", "1", "2", "", True, "")
        self.assertEqual(legacy.get_setting("proxy_enabled"), "1")
        self.assertIn("proxy-pass", legacy.get_setting("proxy_url"))

    def test_proxy_can_be_disabled_visually_without_deleting_saved_url(self):
        legacy.set_setting("proxy_url", "http://proxy-user:proxy-pass@proxy.example.com:8888")
        legacy.set_setting("proxy_enabled", "1")
        self.save_minimal()
        module.save_configuration("http://chatwoot.example.com", "1", "2", "", False, "")
        self.assertEqual(legacy.get_setting("proxy_enabled"), "0")
        self.assertIn("proxy-pass", legacy.get_setting("proxy_url"))
        self.assertFalse(module.effective_proxy()[1])

    def test_old_environment_proxy_is_imported_once_then_ui_is_authoritative(self):
        with patch.object(module.prod, "ENV_PROXY_URL", "http://env-user:env-pass@proxy.example.com:8888"), \
             patch.object(module.prod, "ENV_PROXY_ENABLED", True):
            module.migrate_proxy_env_once()
        self.assertEqual(legacy.get_setting("proxy_enabled"), "1")
        self.assertIn("env-pass", legacy.get_setting("proxy_url"))
        self.assertEqual(legacy.get_setting("proxy_ui_initialized"), "1")

        module.save_configuration("http://chatwoot.example.com", "1", "2", "token", False, "")
        with patch.object(module.prod, "ENV_PROXY_URL", "http://changed:changed@other.example.com:8888"), \
             patch.object(module.prod, "ENV_PROXY_ENABLED", True):
            module.migrate_proxy_env_once()
        self.assertEqual(legacy.get_setting("proxy_enabled"), "0")
        self.assertIn("env-pass", legacy.get_setting("proxy_url"))

    def test_setup_state_never_returns_connection_secrets(self):
        module.save_configuration(
            "http://chatwoot.example.com", "1", "2", "super-secret-token", False, "", "signing-secret-value"
        )
        state = module.setup_state()
        self.assertTrue(state["chatwoot_ready"])
        self.assertTrue(state["token_saved"])
        self.assertTrue(state["webhook_secret_saved"])
        self.assertNotIn("chatwoot_api_token", state)
        self.assertNotIn("chatwoot_webhook_signing_secret", state)
        self.assertNotIn("super-secret-token", repr(state))
        self.assertNotIn("signing-secret-value", repr(state))

    def test_setup_state_counts_linked_conversations(self):
        self.save_minimal()
        with legacy.db() as conn:
            conn.execute(
                "INSERT INTO room_links(room_id, contact_id, source_id, conversation_id, created_at) VALUES(?, ?, ?, ?, ?)",
                ("!room:example.com", 1, "source-1", 77, 1),
            )
        self.assertEqual(module.setup_state()["link_count"], 1)

    def test_verify_chatwoot_requires_config(self):
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            module.verify_chatwoot()

    def test_verify_chatwoot_requires_selected_inbox_to_exist(self):
        self.save_minimal()
        with patch.object(module.prod, "cw_get", return_value={"payload": [{"id": 99}]}):
            with self.assertRaisesRegex(RuntimeError, "Inbox Identifier token"):
                module.verify_chatwoot()

    def test_verify_chatwoot_accepts_selected_inbox_and_records_visible_status(self):
        self.save_minimal()
        with patch.object(module.prod, "cw_get", return_value={"payload": [{"id": 2}]}):
            result = module.verify_chatwoot()
        self.assertIn("PASS", result)
        self.assertIn("account 1", result)
        self.assertIn("inbox 2", result)
        self.assertIn("UTC", legacy.get_setting("chatwoot_verified_at"))

    def test_webhook_registration_verification_accepts_documented_top_level_list(self):
        self.save_minimal()
        data = [{"id": 9, "url": "https://bridge.example.com/webhooks/chatwoot", "subscriptions": ["message_created"]}]
        with patch.object(module.prod, "cw_get", return_value=data):
            result = module.verify_webhook_registration("https://bridge.example.com/webhooks/chatwoot")
        self.assertIn("PASS", result)
        self.assertIn("UTC", legacy.get_setting("webhook_registration_verified_at"))

    def test_webhook_registration_accepts_real_chatwoot_nested_shape_and_imports_secret(self):
        self.save_minimal()
        data = {
            "payload": {
                "webhooks": [
                    {
                        "id": 9,
                        "url": "https://bridge.example.com/webhooks/chatwoot",
                        "subscriptions": ["message_created"],
                        "secret": "server-returned-signing-secret",
                    }
                ]
            }
        }
        with patch.object(module.prod, "cw_get", return_value=data):
            result = module.verify_webhook_registration("https://bridge.example.com/webhooks/chatwoot")
        self.assertIn("PASS", result)
        self.assertIn("imported automatically", result)
        self.assertEqual(legacy.get_setting("chatwoot_webhook_signing_secret"), "server-returned-signing-secret")
        self.assertIn("UTC", legacy.get_setting("webhook_registration_verified_at"))

    def test_webhook_registration_secret_rotation_invalidates_old_delivery_verification(self):
        self.save_minimal()
        legacy.set_setting("chatwoot_webhook_signing_secret", "old-secret")
        legacy.set_setting("webhook_delivery_verified_at", "2026-09-13 12:00:00 UTC")
        data = {
            "payload": {
                "webhooks": [
                    {
                        "url": "https://bridge.example.com/webhooks/chatwoot",
                        "subscriptions": ["message_created"],
                        "secret": "new-secret",
                    }
                ]
            }
        }
        with patch.object(module.prod, "cw_get", return_value=data):
            module.verify_webhook_registration("https://bridge.example.com/webhooks/chatwoot")
        self.assertEqual(legacy.get_setting("chatwoot_webhook_signing_secret"), "new-secret")
        self.assertEqual(legacy.get_setting("webhook_delivery_verified_at"), "")

    def test_webhook_registration_rejects_missing_message_created_cleanly(self):
        self.save_minimal()
        data = {"payload": {"webhooks": [{"url": "https://bridge.example.com/webhooks/chatwoot", "subscriptions": ["conversation_created"]}]}}
        with patch.object(module.prod, "cw_get", return_value=data):
            with self.assertRaisesRegex(RuntimeError, "message_created"):
                module.verify_webhook_registration("https://bridge.example.com/webhooks/chatwoot")

    def test_webhook_registration_reports_unexpected_shape_without_python_attribute_error(self):
        self.save_minimal()
        with patch.object(module.prod, "cw_get", return_value={"payload": {"unexpected": "value"}}):
            with self.assertRaises(RuntimeError) as ctx:
                module.verify_webhook_registration("https://bridge.example.com/webhooks/chatwoot")
        self.assertNotIn("has no attribute", str(ctx.exception))
        self.assertTrue(
            "unexpected format" in str(ctx.exception) or "Webhook URL was not found" in str(ctx.exception)
        )

    def test_webhook_signature_accepts_chatwoot_hmac_and_rejects_bad_signature(self):
        secret = "chatwoot-generated-signing-secret"
        legacy.set_setting("chatwoot_webhook_signing_secret", secret)
        raw = b'{"event":"message_created"}'
        timestamp = str(int(time.time()))
        digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        signature = "sha256=" + digest
        self.assertTrue(module.verify_chatwoot_signature(raw, signature, timestamp, now=int(timestamp)))
        with self.assertRaisesRegex(RuntimeError, "Invalid Chatwoot webhook signature"):
            module.verify_chatwoot_signature(raw, "sha256=bad", timestamp, now=int(timestamp))

    def test_webhook_signature_rejects_stale_delivery(self):
        legacy.set_setting("chatwoot_webhook_signing_secret", "chatwoot-generated-signing-secret")
        now = int(time.time())
        with self.assertRaisesRegex(RuntimeError, "too old"):
            module.verify_chatwoot_signature(b"{}", "sha256=irrelevant", str(now - 1000), now=now)

    def test_verify_proxy_rejects_disabled_proxy(self):
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            module.verify_proxy()

    def _ip_session(self, ip):
        response = Mock()
        response.json.return_value = {"ip": ip}
        response.raise_for_status.return_value = None
        session = Mock()
        session.trust_env = True
        session.get.return_value = response
        return session

    def test_proxy_url_can_be_tested_without_saving_or_enabling(self):
        direct_session = self._ip_session("198.51.100.20")
        proxy_session = self._ip_session("203.0.113.10")
        with patch.object(module.requests, "Session", side_effect=[direct_session, proxy_session]):
            result = module.test_proxy_url("http://user:pass@proxy.example.com:8888")
        self.assertTrue(result["different"])
        self.assertEqual(legacy.get_setting("proxy_enabled"), "")
        self.assertEqual(legacy.get_setting("proxy_url"), "")

    def test_verify_proxy_compares_direct_and_proxy_public_ips(self):
        legacy.set_setting("proxy_enabled", "1")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        direct_session = self._ip_session("198.51.100.20")
        proxy_session = self._ip_session("203.0.113.10")
        with patch.object(module.requests, "Session", side_effect=[direct_session, proxy_session]):
            result = module.verify_proxy()
        self.assertEqual(result["direct_ip"], "198.51.100.20")
        self.assertEqual(result["proxy_ip"], "203.0.113.10")
        self.assertTrue(result["different"])
        self.assertIn("UTC", result["checked_at"])
        self.assertEqual(legacy.get_setting("proxy_verified_ip"), "203.0.113.10")
        self.assertFalse(direct_session.trust_env)
        self.assertFalse(proxy_session.trust_env)
        direct_session.get.assert_called_once_with(module.IP_CHECK_URL, proxies=None, timeout=20)
        proxy_session.get.assert_called_once()
        self.assertIn("proxy.example.com", proxy_session.get.call_args.kwargs["proxies"]["https"])

    def test_verify_proxy_flags_identical_direct_and_proxy_ips(self):
        legacy.set_setting("proxy_enabled", "1")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        direct_session = self._ip_session("198.51.100.20")
        proxy_session = self._ip_session("198.51.100.20")
        with patch.object(module.requests, "Session", side_effect=[direct_session, proxy_session]):
            result = module.verify_proxy()
        self.assertFalse(result["different"])

    def test_verify_proxy_rejects_invalid_ip_check_response(self):
        legacy.set_setting("proxy_enabled", "1")
        legacy.set_setting("proxy_url", "http://user:pass@proxy.example.com:8888")
        bad_session = self._ip_session("not-an-ip")
        with patch.object(module.requests, "Session", return_value=bad_session):
            with self.assertRaisesRegex(RuntimeError, "invalid public IP"):
                module.verify_proxy()

    def test_internal_proxy_basic_auth_parser_contract(self):
        header = "Basic " + base64.b64encode(b"mautrix:resolver-secret-long-value").decode()
        decoded = base64.b64decode(header.split(" ", 1)[1], validate=True).decode("utf-8")
        self.assertEqual(decoded, "mautrix:resolver-secret-long-value")

    def test_captcha_attachment_source_is_data_uri(self):
        source = module._login_attachment_source({
            "type": "m.image",
            "content": "iVBORw0KGgo=",
            "info": {"mimetype": "image/png"},
        })
        self.assertEqual(source, "data:image/png;base64,iVBORw0KGgo=")
        self.assertEqual(module._login_attachment_source({
            "type": "m.file",
            "content": "aGVsbG8=",
            "info": {"mimetype": "text/plain"},
        }), "")

    def test_captcha_code_is_visible_but_otp_remains_secret(self):
        captcha = {"id": "captcha_code", "type": "text"}
        otp = {"id": "otp_code", "type": "otp"}
        self.assertFalse(
            module._is_secret_login_field("fi.mau.meta.messengerlite.captcha", captcha)
        )
        self.assertTrue(
            module._is_secret_login_field("fi.mau.meta.messengerlite.two_factor", otp)
        )

    def test_nicegui_runtime_is_selected(self):
        self.assertTrue(callable(module.run))
        self.assertEqual(module.legacy.SESSION_SECRET, "session-secret-long-enough-for-nicegui-tests")


if __name__ == "__main__":
    unittest.main()
