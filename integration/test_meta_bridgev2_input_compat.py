import base64
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

    def test_captcha_image_attachment_is_preserved_and_mime_detected_from_bytes(self):
        png = b"\x89PNG\r\n\x1a\n" + b"captcha-bytes"
        step = {
            "login_id": "proc",
            "step_id": "fi.mau.meta.messengerlite.captcha",
            "type": "user_input",
            "user_input": {
                "fields": [{"id": "captcha_code", "type": "text"}],
                "attachments": [{
                    "type": "m.image",
                    "filename": "captcha.jpg",
                    "content": base64.b64encode(png).decode(),
                    "info": {"mimetype": "image/jpeg", "size": len(png), "w": 280, "h": 70},
                }],
            },
        }
        safe = mp.safe_step(step)
        attachment = safe["user_input"]["attachments"][0]
        self.assertEqual(attachment["mimetype"], "image/png")
        self.assertEqual(attachment["size"], len(png))
        self.assertEqual(attachment["w"], 280)
        self.assertEqual(attachment["h"], 70)
        self.assertEqual(base64.b64decode(attachment["content"]), png)

    def test_captcha_audio_attachment_is_preserved_and_mime_detected_from_bytes(self):
        ogg = b"OggS" + b"audio-captcha-bytes"
        step = {
            "login_id": "proc",
            "step_id": "fi.mau.meta.messengerlite.captcha",
            "type": "user_input",
            "user_input": {
                "fields": [{"id": "captcha_code", "type": "text"}],
                "attachments": [{
                    "type": "m.audio",
                    "filename": "captcha.bin",
                    "content": base64.b64encode(ogg).decode(),
                    "info": {"mimetype": "application/octet-stream", "size": len(ogg)},
                }],
            },
        }
        safe = mp.safe_step(step)
        attachment = safe["user_input"]["attachments"][0]
        self.assertEqual(attachment["type"], "m.audio")
        self.assertEqual(attachment["mimetype"], "audio/ogg")
        self.assertEqual(attachment["size"], len(ogg))
        self.assertEqual(base64.b64decode(attachment["content"]), ogg)

    def test_real_bridgev2_captcha_shape_keeps_image_and_audio(self):
        png = b"\x89PNG\r\n\x1a\n" + b"image"
        ogg = b"OggS" + b"audio"
        step = {
            "login_id": "proc",
            "step_id": "fi.mau.meta.messengerlite.captcha",
            "type": "user_input",
            "instructions": "Facebook requires solving a captcha",
            "user_input": {
                "attachments": [
                    {"type": "m.image", "filename": "captcha.png", "content": base64.b64encode(png).decode()},
                    {"type": "m.audio", "filename": "captcha.ogg", "content": base64.b64encode(ogg).decode()},
                ],
                "fields": [{"id": "captcha_code", "name": "Captcha code", "type": "captcha_code"}],
            },
        }
        safe = mp.safe_step(step)
        attachments = safe["user_input"]["attachments"]
        self.assertEqual([item["type"] for item in attachments], ["m.image", "m.audio"])
        self.assertEqual([item["mimetype"] for item in attachments], ["image/png", "audio/ogg"])
        self.assertEqual(safe["user_input"]["fields"][0]["id"], "captcha_code")

    def test_invalid_and_oversized_audio_attachments_are_dropped(self):
        oversized = b"OggS" + b"x" * mp.MAX_LOGIN_AUDIO_BYTES
        step = {
            "type": "user_input",
            "user_input": {
                "fields": [],
                "attachments": [
                    {"type": "m.audio", "content": "not-base64!!"},
                    {"type": "m.audio", "content": base64.b64encode(b"not-audio").decode()},
                    {"type": "m.audio", "content": base64.b64encode(oversized).decode()},
                ],
            },
        }
        safe = mp.safe_step(step)
        self.assertNotIn("attachments", safe["user_input"])

    def test_non_image_and_invalid_image_attachments_are_dropped(self):
        step = {
            "login_id": "proc",
            "step_id": "captcha",
            "type": "user_input",
            "user_input": {
                "fields": [{"id": "captcha_code", "type": "text"}],
                "attachments": [
                    {"type": "m.file", "content": base64.b64encode(b"secret").decode()},
                    {"type": "m.image", "content": "not-base64!!"},
                    {"type": "m.image", "content": base64.b64encode(b"not-an-image").decode()},
                ],
            },
        }
        safe = mp.safe_step(step)
        self.assertNotIn("attachments", safe["user_input"])

    def test_oversized_login_image_attachment_is_dropped(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"x" * mp.MAX_LOGIN_IMAGE_BYTES
        step = {
            "type": "user_input",
            "user_input": {
                "fields": [],
                "attachments": [{"type": "m.image", "content": base64.b64encode(raw).decode()}],
            },
        }
        safe = mp.safe_step(step)
        self.assertNotIn("attachments", safe["user_input"])

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
