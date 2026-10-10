import os
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ["ADMIN_BRIDGE_SHARED_SECRET"] = "admin-read-test"

import app


class PaymentAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_unauthorized_admin_cannot_manage_payment_methods(self):
        response = self.client.get("/admin/v1/payment-methods")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")

    def test_unauthorized_admin_cannot_access_payment_asset(self):
        response = self.client.get("/admin/v1/payment-methods/1/asset")
        self.assertEqual(response.status_code, 401)

    def test_invalid_rate_definition_is_rejected_before_database(self):
        response = self.client.put(
            "/admin/v1/payment-methods/1/directions",
            headers={"X-Admin-Bridge-Key": "admin-read-test"},
            json={"direction": "deposit", "rate": "100", "rate_definition": "100"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "rate_definition_must_describe_units")

    def test_authorized_create_requires_postgres_but_does_not_use_fake_defaults(self):
        response = self.client.post(
            "/admin/v1/payment-methods",
            headers={"X-Admin-Bridge-Key": "admin-read-test"},
            json={"action": "create", "method_key": "local_method", "display_name": "Local"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "postgres_required")


if __name__ == "__main__":
    unittest.main()
