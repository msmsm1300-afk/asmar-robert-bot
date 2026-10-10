import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("BOT_TOKEN", "test-bot-token")

import app


class FakeTelegramResponse:
    status_code = 400

    def json(self):
        return {
            "ok": False,
            "error_code": 400,
            "description": "sensitive-response-body SECRET_TOKEN cookie=abc123",
            "token": "test-secret-token",
            "trace_id": "trace-123",
        }


class TelegramLogRedactionTests(unittest.TestCase):
    def setUp(self):
        self.old_bot_token = app.BOT_TOKEN
        self.old_tg_api = app.TG_API
        app.BOT_TOKEN = "test-bot-token"
        app.TG_API = "https://telegram.invalid/bottest"

    def tearDown(self):
        app.BOT_TOKEN = self.old_bot_token
        app.TG_API = self.old_tg_api

    def test_tg_logs_metadata_only(self):
        output = io.StringIO()
        with patch.object(app.TG_SESSION, "post", return_value=FakeTelegramResponse()), contextlib.redirect_stdout(output):
            result = app.tg("sendMessage", {"chat_id": "user-1", "text": "secret user text"})
        self.assertFalse(result["ok"])
        logged = output.getvalue()
        self.assertIn("method=sendMessage", logged)
        self.assertIn("http=400", logged)
        self.assertIn("code=400", logged)
        self.assertIn("trace=trace-123", logged)
        self.assertNotIn("sensitive-response-body", logged)
        self.assertNotIn("SECRET_TOKEN", logged)
        self.assertNotIn("cookie=abc123", logged)
        self.assertNotIn("test-secret-token", logged)

    def test_send_photo_logs_metadata_only(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg") as photo:
            photo.write(b"test image")
            photo.flush()
            output = io.StringIO()
            with patch.object(app.TG_SESSION, "post", return_value=FakeTelegramResponse()), contextlib.redirect_stdout(output):
                result = app.send_photo_file(1, photo.name, "caption")
        self.assertFalse(result["ok"])
        logged = output.getvalue()
        self.assertIn("method=sendPhoto", logged)
        self.assertNotIn("sensitive-response-body", logged)
        self.assertNotIn("SECRET_TOKEN", logged)
        self.assertNotIn("cookie=abc123", logged)
        self.assertNotIn("test-secret-token", logged)


if __name__ == "__main__":
    unittest.main()
