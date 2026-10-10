import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

import app


SENSITIVE = "SECRET_TOKEN cookie=private-cookie Authorization=Bearer-private request-body"


class ExceptionLogRedactionTests(unittest.TestCase):
    def setUp(self):
        self.old_bot_token = app.BOT_TOKEN
        self.old_webhook_secret = app.WEBHOOK_SECRET
        app.BOT_TOKEN = "local-test-token"
        app.WEBHOOK_SECRET = "local-webhook-secret"

    def tearDown(self):
        app.BOT_TOKEN = self.old_bot_token
        app.WEBHOOK_SECRET = self.old_webhook_secret

    def test_telegram_exception_has_generic_response_and_safe_log(self):
        output = io.StringIO()
        with patch.object(app.TG_SESSION, "post", side_effect=RuntimeError(SENSITIVE)), contextlib.redirect_stdout(output):
            result = app.tg("sendMessage", {"text": SENSITIVE})
        self.assertEqual(result["description"], "telegram_request_failed")
        self.assertRegex(result["trace_id"], r"^[0-9a-f]{12}$")
        logged = output.getvalue()
        self.assertIn("operation=telegram.sendMessage", logged)
        self.assertIn("type=RuntimeError", logged)
        self.assertNotIn(SENSITIVE, logged)
        self.assertNotIn("SECRET_TOKEN", logged)
        self.assertNotIn("private-cookie", logged)

    def test_send_photo_exception_has_generic_response_and_safe_log(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as photo:
            photo.write(b"local image")
            photo.flush()
            output = io.StringIO()
            with patch.object(app.TG_SESSION, "post", side_effect=RuntimeError(SENSITIVE)), contextlib.redirect_stdout(output):
                result = app.send_photo_file(1, photo.name, SENSITIVE)
        self.assertEqual(result["description"], "telegram_request_failed")
        self.assertNotIn(SENSITIVE, output.getvalue())

    def test_callback_exception_does_not_log_callback_data_or_traceback(self):
        callback_data = f"admin-action:{SENSITIVE}"
        output = io.StringIO()
        with patch.object(app, "handle_callback", side_effect=RuntimeError(SENSITIVE)), \
             patch.object(app, "answer_callback"), contextlib.redirect_stdout(output):
            response = app.app.test_client().post(
                "/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": app.WEBHOOK_SECRET},
                json={"callback_query": {"id": "local-callback", "data": callback_data}},
            )
        self.assertEqual(response.status_code, 200)
        logged = output.getvalue()
        self.assertIn("operation=webhook.callback", logged)
        self.assertIn("callback handled=1", logged)
        self.assertNotIn(callback_data, logged)
        self.assertNotIn(SENSITIVE, logged)
        self.assertNotIn("Traceback", logged)

    def test_message_exception_returns_generic_user_message(self):
        output = io.StringIO()
        with patch.object(app, "upsert_user"), \
             patch.object(app, "process_text_input", side_effect=RuntimeError(SENSITIVE)), \
             patch.object(app, "send_message") as send_message, contextlib.redirect_stdout(output):
            response = app.app.test_client().post(
                "/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": app.WEBHOOK_SECRET},
                json={"message": {"chat": {"id": 987654}, "from": {"id": 987654}, "text": "hello"}},
            )
        self.assertEqual(response.status_code, 200)
        send_message.assert_called_once()
        self.assertIn("حدث خطأ مؤقت", send_message.call_args.args[1])
        self.assertNotIn(SENSITIVE, send_message.call_args.args[1])
        self.assertNotIn(SENSITIVE, output.getvalue())


if __name__ == "__main__":
    unittest.main()
