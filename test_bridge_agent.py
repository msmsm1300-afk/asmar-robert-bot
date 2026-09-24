import os
import unittest

os.environ.setdefault("BRIDGE_SHARED_SECRET", "test-secret")

from app import app


class BridgeRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_status_is_readable_without_database(self):
        response = self.client.get("/bridge/status")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_heartbeat_requires_bridge_key(self):
        response = self.client.post("/bridge/v1/heartbeat", json={"device_id": "d1", "device_name": "test"})
        self.assertEqual(response.status_code, 401)

    def test_heartbeat_is_outbound_contract(self):
        response = self.client.post(
            "/bridge/v1/heartbeat",
            headers={"X-Bridge-Key": "test-secret"},
            json={"device_id": "d1", "device_name": "test", "ichancy_connected": False},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["error"], "postgres_required")


if __name__ == "__main__":
    unittest.main()
