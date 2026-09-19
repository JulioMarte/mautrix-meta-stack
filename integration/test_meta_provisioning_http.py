import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import meta_provisioning as mp


class ContractHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []

    def log_message(self, *_args):
        pass

    def _reply(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("content-length") or "0")
        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = None
        entry = {
            "method": self.command,
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "authorization": self.headers.get("authorization"),
            "payload": payload,
        }
        type(self).requests.append(entry)
        return entry

    def _authorize(self, entry):
        if entry["authorization"] != "Bearer integration-contract-secret":
            self._reply(401, {"errcode": "M_UNKNOWN_TOKEN", "error": "Invalid auth token"})
            return False
        if entry["query"].get("user_id") != ["@admin:matrix.example.com"]:
            self._reply(403, {"errcode": "M_FORBIDDEN", "error": "Missing user_id"})
            return False
        return True

    def do_GET(self):
        entry = self._record()
        if not self._authorize(entry):
            return
        if entry["path"].endswith("/v3/login/flows"):
            self._reply(200, {"flows": [
                {"id": "messenger-lite-android", "name": "Messenger Android"},
                {"id": "facebook", "name": "facebook.com"},
                {"id": "messenger", "name": "messenger.com"},
            ]})
        elif entry["path"].endswith("/v3/whoami"):
            self._reply(200, {"network": {"display_name": "Meta"}, "logins": []})
        elif entry["path"].endswith("/v3/logins"):
            self._reply(200, {"login_ids": []})
        else:
            self._reply(404, {"errcode": "M_UNRECOGNIZED", "error": "unknown"})

    def do_POST(self):
        entry = self._record()
        if not self._authorize(entry):
            return
        path = entry["path"]
        if path.endswith("/v3/login/start/messenger-lite-android"):
            self._reply(200, {
                "login_id": "android/login-process",
                "step_id": "fi.mau.meta.login",
                "txn_id": "android-txn-1",
                "type": "user_input",
                "user_input": {
                    "fields": [
                        {"id": "email", "name": "Correo o teléfono", "type": "text", "required": True},
                        {"id": "password", "name": "Contraseña", "type": "password", "required": True},
                    ],
                },
            })
        elif path.endswith("/v3/login/start/facebook"):
            self._reply(200, {
                "login_id": "process/with slash",
                "step_id": "fi.mau.meta.cookies",
                "txn_id": "txn/1",
                "type": "cookies",
                "cookies": {
                    "url": "https://www.facebook.com/",
                    "fields": [
                        {"id": "c_user", "required": True},
                        {"id": "xs", "required": True},
                    ],
                },
            })
        elif "/v3/login/step/" in path and path.endswith("/user_input"):
            self._reply(200, {
                "type": "complete",
                "login_id": "android/login-process",
                "step_id": "fi.mau.meta.complete",
            })
        elif "/v3/login/step/" in path and path.endswith("/cookies"):
            self._reply(200, {"type": "complete", "step_id": "fi.mau.meta.complete"})
        elif "/v3/login/cancel/" in path:
            self._reply(200, {})
        elif "/v3/logout/" in path:
            self._reply(200, {})
        else:
            self._reply(404, {"errcode": "M_UNRECOGNIZED", "error": "unknown"})


class ProvisioningHTTPContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ContractHandler.requests = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ContractHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        port = cls.server.server_address[1]
        cls.config = mp.ProvisioningConfig(
            base_url=f"http://127.0.0.1:{port}/_matrix/provision",
            user_id="@admin:matrix.example.com",
            shared_secret="integration-contract-secret",
            timeout=3,
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        ContractHandler.requests.clear()
        self.client = mp.MautrixProvisioningClient(self.config)

    def test_full_cookie_flow_uses_exact_http_contract(self):
        flows = self.client.flows()
        self.assertEqual(
            [flow["id"] for flow in flows],
            ["messenger-lite-android", "facebook", "messenger"],
        )

        step = self.client.start("facebook", existing_login_id="existing/login")
        self.assertEqual(step["type"], "cookies")
        self.assertEqual(step["login_id"], "process/with slash")

        done = self.client.submit_cookies_trusted(
            step["login_id"],
            step["step_id"],
            {"c_user": "123", "xs": "abc"},
            txn_id=step["txn_id"],
        )
        self.assertEqual(done["type"], "complete")
        self.client.cancel(step["login_id"])
        self.client.logout("all")

        start = ContractHandler.requests[1]
        self.assertEqual(start["query"]["login_id"], ["existing/login"])
        submit = ContractHandler.requests[2]
        self.assertIn("process%2Fwith%20slash", submit["path"])
        self.assertEqual(submit["query"]["txn_id"], ["txn/1"])
        self.assertEqual(submit["payload"], {"c_user": "123", "xs": "abc"})
        self.assertTrue(all(r["authorization"] == "Bearer integration-contract-secret" for r in ContractHandler.requests))

    def test_recommended_android_flow_uses_user_input_contract(self):
        step = self.client.start("messenger-lite-android")
        self.assertEqual(step["type"], "user_input")
        self.assertEqual(step["login_id"], "android/login-process")
        self.assertEqual(
            [field["id"] for field in step["user_input"]["fields"]],
            ["email", "password"],
        )

        done = self.client.submit_user_input(
            step["login_id"],
            step["step_id"],
            {"email": "person@example.com", "password": "not-a-real-password"},
            txn_id=step["txn_id"],
        )
        self.assertEqual(done["type"], "complete")

        start = ContractHandler.requests[0]
        submit = ContractHandler.requests[1]
        self.assertTrue(start["path"].endswith("/v3/login/start/messenger-lite-android"))
        self.assertIn("android%2Flogin-process", submit["path"])
        self.assertEqual(submit["query"]["txn_id"], ["android-txn-1"])
        self.assertEqual(
            submit["payload"],
            {"email": "person@example.com", "password": "not-a-real-password"},
        )

    def test_real_http_rejects_bad_secret_without_leaking_it(self):
        bad = mp.MautrixProvisioningClient(mp.ProvisioningConfig(
            base_url=self.config.base_url,
            user_id=self.config.user_id,
            shared_secret="wrong-secret-long-enough",
            timeout=3,
        ))
        with self.assertRaises(mp.ProvisioningError) as ctx:
            bad.whoami()
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.errcode, "M_UNKNOWN_TOKEN")
        self.assertNotIn("wrong-secret-long-enough", str(ctx.exception))

    def test_operator_message_explains_internal_bridge_error_without_blame(self):
        exc = mp.ProvisioningError(
            "Internal error in login step",
            errcode="M_UNKNOWN",
            status_code=500,
        )
        message = mp.operator_error_message(exc)
        self.assertIn("error interno", message)
        self.assertIn("incompatibilidad", message)
        self.assertIn("no significa", message)
        self.assertIn("mautrix-meta", message)
        self.assertNotIn("contraseña incorrecta", message.lower())

    def test_operator_message_keeps_specific_non_internal_error(self):
        exc = mp.ProvisioningError(
            "Facebook rejected this step",
            errcode="M_FORBIDDEN",
            status_code=400,
        )
        message = mp.operator_error_message(exc)
        self.assertIn("Facebook rejected this step", message)
        self.assertIn("META_LOGIN_REJECTED", message)

    def test_operator_error_exposes_stable_code_reference_and_retryability(self):
        exc = mp.ProvisioningError(
            "rate limited",
            errcode="M_LIMIT_EXCEEDED",
            status_code=429,
            trace_id="trace-abc123",
            retryable=True,
        )
        message = mp.operator_error_message(exc)
        self.assertIn("META_RATE_LIMITED", message)
        self.assertIn("trace-abc123", message)
        self.assertIn("reintentable", message)
        self.assertEqual(mp.operator_error_code(exc), "META_RATE_LIMITED")

    def test_real_http_error_log_has_trace_duration_and_stable_failure_code(self):
        bad = mp.MautrixProvisioningClient(mp.ProvisioningConfig(
            base_url=self.config.base_url,
            user_id=self.config.user_id,
            shared_secret="wrong-secret-long-enough",
            timeout=3,
        ))
        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(mp.ProvisioningError) as ctx:
            bad.whoami()
        output = stream.getvalue()
        self.assertTrue(ctx.exception.trace_id)
        self.assertEqual(ctx.exception.failure_code, "META_PROVISIONING_AUTH")
        self.assertIn(f'"trace_id":"{ctx.exception.trace_id}"', output)
        self.assertIn('"failure_code":"META_PROVISIONING_AUTH"', output)
        self.assertIn('"duration_ms":', output)
        self.assertNotIn("wrong-secret-long-enough", output)

    def test_debug_logging_redacts_payload_secrets_and_temporary_ids(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            mp.provisioning_debug(
                "test",
                path="/v3/login/step/raw-login-id/raw-step-id/user_input",
                login_id="raw-login-id",
                txn_id="raw-txn-id",
                payload={"password": "super-secret-password"},
                values={"otp": "123456"},
                authorization="Bearer super-secret-token",
                status_code=401,
                errcode="M_FORBIDDEN",
            )
        output = stream.getvalue()
        self.assertIn("META_LOGIN_DEBUG", output)
        self.assertIn("/v3/login/step/{login_id}/{step_id}/user_input", output)
        self.assertIn('"status_code":401', output)
        self.assertIn('"errcode":"M_FORBIDDEN"', output)
        for secret in (
            "raw-login-id", "raw-step-id", "raw-txn-id", "super-secret-password",
            "123456", "super-secret-token",
        ):
            self.assertNotIn(secret, output)

    def test_whoami_and_logins_over_real_socket(self):
        self.assertEqual(self.client.whoami()["network"]["display_name"], "Meta")
        self.assertEqual(self.client.logins(), [])


if __name__ == "__main__":
    unittest.main()
