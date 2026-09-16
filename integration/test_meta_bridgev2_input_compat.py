import unittest

import meta_provisioning as mp


class BridgeV2InputCompatibilityTests(unittest.TestCase):
    def test_string_select_options_are_normalized_for_existing_ui(self):
        step = {
            "login_id": "proc",
            "step_id": "checkpoint",
            "type": "user_input",
            "user_input": {
                "fields": [{
                    "id": "method",
                    "name": "Verification method",
                    "type": "select",
                    "options": ["sms", "email"],
                }],
            },
        }
        safe = mp.safe_step(step)
        field = safe["user_input"]["fields"][0]
        self.assertEqual(field["options"], [
            {"id": "sms", "name": "sms"},
            {"id": "email", "name": "email"},
        ])

    def test_object_select_options_keep_labels(self):
        step = {
            "login_id": "proc",
            "step_id": "checkpoint",
            "type": "user_input",
            "user_input": {
                "fields": [{
                    "id": "method",
                    "type": "select",
                    "options": [
                        {"id": "sms", "name": "Text message"},
                        {"id": "email"},
                    ],
                }],
            },
        }
        safe = mp.safe_step(step)
        self.assertEqual(safe["user_input"]["fields"][0]["options"], [
            {"id": "sms", "name": "Text message"},
            {"id": "email", "name": "email"},
        ])

    def test_otp_and_token_fields_are_rendered_as_sensitive_inputs(self):
        for upstream_type in ("2fa_code", "token", "secret"):
            with self.subTest(upstream_type=upstream_type):
                step = {
                    "login_id": "proc",
                    "step_id": "checkpoint",
                    "type": "user_input",
                    "user_input": {
                        "fields": [{"id": "challenge", "type": upstream_type}],
                    },
                }
                safe = mp.safe_step(step)
                self.assertEqual(safe["user_input"]["fields"][0]["type"], "password")

    def test_no_input_values_are_ever_persisted_by_safe_step(self):
        step = {
            "login_id": "proc",
            "step_id": "password",
            "type": "user_input",
            "user_input": {
                "fields": [{"id": "password", "type": "password"}],
                "password": "must-not-survive",
                "values": {"password": "also-must-not-survive"},
            },
        }
        rendered = repr(mp.safe_step(step))
        self.assertNotIn("must-not-survive", rendered)
        self.assertNotIn("also-must-not-survive", rendered)


if __name__ == "__main__":
    unittest.main()
