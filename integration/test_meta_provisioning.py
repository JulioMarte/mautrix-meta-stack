import os
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

import meta_provisioning as mp


class MetaProvisioningTests(unittest.TestCase):
    def _clear_secret_env(self):
        for key in ("MAUTRIX_PROVISIONING_SECRET", "MAUTRIX_PROVISIONING_SECRET_PATH", "MAUTRIX_CONFIG_PATH"):
            os.environ.pop(key, None)

    def test_load_shared_secret_from_isolated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = os.path.join(tmp, "shared_secret")
            with open(secret_path, "w", encoding="utf-8") as handle:
                handle.write("isolated-secret-long-enough\n")
            with patch.dict(os.environ, {"MAUTRIX_PROVISIONING_SECRET_PATH": secret_path}, clear=False):
                os.environ.pop("MAUTRIX_PROVISIONING_SECRET", None)
                self.assertEqual(mp.load_shared_secret(), "isolated-secret-long-enough")

    def test_env_secret_overrides_isolated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = os.path.join(tmp, "shared_secret")
            with open(secret_path, "w", encoding="utf-8") as handle:
                handle.write("isolated-secret-long-enough")
            with patch.dict(os.environ, {
                "MAUTRIX_PROVISIONING_SECRET_PATH": secret_path,
                "MAUTRIX_PROVISIONING_SECRET": "explicit-secret-long-enough",
            }, clear=False):
                self.assertEqual(mp.load_shared_secret(), "explicit-secret-long-enough")

    def test_load_shared_secret_falls_back_to_private_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_secret_path = os.path.join(tmp, "missing-secret")
            config_path = os.path.join(tmp, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"provisioning": {"shared_secret": "a-very-long-private-secret"}}, handle)
            with patch.dict(os.environ, {
                "MAUTRIX_PROVISIONING_SECRET_PATH": missing_secret_path,
                "MAUTRIX_CONFIG_PATH": config_path,
            }, clear=False):
                os.environ.pop("MAUTRIX_PROVISIONING_SECRET", None)
                self.assertEqual(mp.load_shared_secret(), "a-very-long-private-secret")

    def test_invalid_isolated_secret_fails_closed_without_config_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = os.path.join(tmp, "shared_secret")
            config_path = os.path.join(tmp, "config.yaml")
            with open(secret_path, "w", encoding="utf-8") as handle:
                handle.write("short")
            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"provisioning": {"shared_secret": "valid-config-secret-long-enough"}}, handle)
            with patch.dict(os.environ, {
                "MAUTRIX_PROVISIONING_SECRET_PATH": secret_path,
                "MAUTRIX_CONFIG_PATH": config_path,
            }, clear=False):
                os.environ.pop("MAUTRIX_PROVISIONING_SECRET", None)
                with self.assertRaises(mp.ProvisioningError):
                    mp.load_shared_secret()

    def test_uninitialized_config_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yaml")
            missing_secret_path = os.path.join(tmp, "missing-secret")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"provisioning": {"shared_secret": "generate"}}, handle)
            with patch.dict(os.environ, {
                "MAUTRIX_PROVISIONING_SECRET_PATH": missing_secret_path,
                "MAUTRIX_CONFIG_PATH": path,
            }, clear=False):
                os.environ.pop("MAUTRIX_PROVISIONING_SECRET", None)
                with self.assertRaises(mp.ProvisioningError):
                    mp.load_shared_secret()

    def make_client(self):
        session = Mock()
        session.trust_env = True
        config = mp.ProvisioningConfig(
            base_url="http://mautrix-meta:29319/_matrix/provision",
            user_id="@admin:matrix.example.com",
            shared_secret="shared-secret-long-enough",
            timeout=9,
        )
        return mp.MautrixProvisioningClient(config, session), session

    def response(self, status=200, data=None, content=b"json"):
        response = Mock()
        response.status_code = status
        response.content = content
        response.json.return_value = {} if data is None else data
        return response

    def test_client_uses_private_bearer_and_user_id(self):
        client, session = self.make_client()
        session.request.return_value = self.response(data={"flows": [{"id": "facebook"}]})
        flows = client.flows()
        self.assertEqual(flows[0]["id"], "facebook")
        self.assertFalse(session.trust_env)
        call = session.request.call_args
        self.assertEqual(call.args[:2], ("GET", "http://mautrix-meta:29319/_matrix/provision/v3/login/flows"))
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer shared-secret-long-enough")
        self.assertEqual(call.kwargs["params"]["user_id"], "@admin:matrix.example.com")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["timeout"], 9)

    def test_start_and_submit_use_versioned_v3_paths(self):
        client, session = self.make_client()
        session.request.side_effect = [
            self.response(data={"login_id": "process/1", "step_id": "step/1", "type": "user_input"}),
            self.response(data={"login_id": "process/1", "step_id": "done", "type": "complete"}),
        ]
        step = client.start("messenger-lite")
        self.assertEqual(step["type"], "user_input")
        client.submit_user_input("process/1", "step/1", {"username": "user", "password": "secret"})
        self.assertIn("/v3/login/start/messenger-lite", session.request.call_args_list[0].args[1])
        self.assertIn("/v3/login/step/process%2F1/step%2F1/user_input", session.request.call_args_list[1].args[1])
        self.assertEqual(session.request.call_args_list[1].kwargs["json"]["password"], "secret")

    def test_safe_step_preserves_captcha_image_and_detects_real_png_mime(self):
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAusB9Y9ZQmcAAAAASUVORK5CYII="
        )
        step = {
            "login_id": "proc",
            "step_id": "fi.mau.meta.messengerlite.captcha",
            "type": "user_input",
            "instructions": "Facebook requires solving a captcha",
            "user_input": {
                "fields": [{"id": "captcha_code", "name": "Captcha code", "type": "text"}],
                "attachments": [{
                    "type": "m.image",
                    "content": png_b64,
                    "filename": "captcha.jpg",
                    "info": {"mimetype": "image/jpeg", "size": 9999, "w": 280, "h": 70},
                }],
            },
        }
        safe = mp.safe_step(step)
        attachments = safe["user_input"]["attachments"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["content"], png_b64)
        self.assertEqual(attachments[0]["info"]["mimetype"], "image/png")
        self.assertGreater(attachments[0]["info"]["size"], 0)

    def test_safe_step_rejects_non_image_or_invalid_base64_attachments(self):
        step = {
            "type": "user_input",
            "user_input": {
                "fields": [],
                "attachments": [
                    {"type": "m.file", "content": "aGVsbG8=", "info": {"mimetype": "text/plain"}},
                    {"type": "m.image", "content": "not-base64!!!", "info": {"mimetype": "image/png"}},
                    {"type": "m.image", "content": "aGVsbG8=", "info": {"mimetype": "image/png"}},
                ],
            },
        }
        safe = mp.safe_step(step)
        self.assertNotIn("attachments", safe["user_input"])

    def test_safe_step_drops_sensitive_payloads(self):
        step = {
            "login_id": "proc",
            "step_id": "cookie-step",
            "txn_id": "txn",
            "type": "cookies",
            "instructions": "Sign in",
            "cookies": {
                "url": "https://www.facebook.com/",
                "user_agent": "ua",
                "fields": [{"id": "c_user", "required": True, "sources": [{"cookie": "do-not-persist"}]}],
            },
            "client_http": {"headers": {"Authorization": "secret"}},
            "webauthn": {"challenge": "secret-challenge"},
            "password": "never-store-me",
        }
        safe = mp.safe_step(step)
        rendered = repr(safe)
        self.assertEqual(safe["cookies"]["url"], "https://www.facebook.com/")
        self.assertEqual(safe["cookies"]["fields"], [{"id": "c_user", "required": True}])
        self.assertNotIn("do-not-persist", rendered)
        self.assertNotIn("secret-challenge", rendered)
        self.assertNotIn("never-store-me", rendered)
        self.assertNotIn("client_http", safe)
        self.assertNotIn("webauthn", safe)

    def test_error_is_normalized_without_echoing_authorization(self):
        client, session = self.make_client()
        session.request.return_value = self.response(
            status=401,
            data={"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid auth token"},
        )
        with self.assertRaises(mp.ProvisioningError) as ctx:
            client.whoami()
        self.assertEqual(ctx.exception.errcode, "M_UNKNOWN_TOKEN")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("shared-secret-long-enough", str(ctx.exception))

    def test_redirects_fail_closed(self):
        client, session = self.make_client()
        session.request.return_value = self.response(status=302, data={})
        with self.assertRaisesRegex(mp.ProvisioningError, "redirect"):
            client.whoami()

    def test_connection_summary(self):
        self.assertEqual(mp.connection_summary({"logins": []})["status"], "disconnected")
        connected = mp.connection_summary({"logins": [{"id": "fb-1", "name": "Julio", "state": {"state_event": "CONNECTED"}}]})
        self.assertTrue(connected["connected"])
        self.assertEqual(connected["logins"][0]["name"], "Julio")
        bad = mp.connection_summary({"logins": [{"id": "fb-1", "state": {"state_event": "BAD_CREDENTIALS"}}]})
        self.assertEqual(bad["status"], "action_required")


if __name__ == "__main__":
    unittest.main()
