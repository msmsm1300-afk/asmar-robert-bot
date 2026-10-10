import unittest
from unittest.mock import patch

import app


class SecurityEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.previous_key = app.ADMIN_BRIDGE_SHARED_SECRET
        app.ADMIN_BRIDGE_SHARED_SECRET = "local-admin-key"

    def tearDown(self):
        app.ADMIN_BRIDGE_SHARED_SECRET = self.previous_key

    def test_diagnostics_requires_admin_key_and_does_not_query_telegram(self):
        self.assertEqual(self.client.get("/diagnostics").status_code, 401)
        with patch.object(app, "tg", side_effect=AssertionError("Telegram must not be queried")):
            response = self.client.get("/diagnostics", headers={"X-Admin-Bridge-Key": "local-admin-key"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["telegram_queried"])

    def test_set_webhook_requires_admin_key(self):
        self.assertEqual(self.client.get("/set-webhook").status_code, 401)

    def test_authorized_set_webhook_only_uses_local_mocks(self):
        calls = []
        with patch.object(app, "ensure_native_menu", return_value=None), patch.object(
            app, "tg", side_effect=lambda method, payload=None: calls.append(method) or {"ok": True}
        ):
            response = self.client.get("/set-webhook", headers={"X-Admin-Bridge-Key": "local-admin-key"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ["setWebhook"])


if __name__ == "__main__":
    unittest.main()
