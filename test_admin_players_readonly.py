import os
import unittest
from unittest.mock import patch

os.environ.pop("DATABASE_URL", None)
os.environ["ADMIN_BRIDGE_SHARED_SECRET"] = "admin-read-test"
os.environ["BRIDGE_SHARED_SECRET"] = "test-secret"

from app import app, enqueue_admin_players_read


class AdminPlayersReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_endpoint_requires_separate_admin_key(self):
        response = self.client.post(
            "/admin/v1/bridge/players",
            json={"operation_ref": "read-1"},
        )
        self.assertEqual(response.status_code, 401)

    def test_endpoint_accepts_only_operation_ref_and_never_financial_type(self):
        with patch("app.enqueue_admin_players_read", return_value={
            "ok": True,
            "job_id": "job-1",
            "request_id": "admin-get-players:read-1",
            "status": "pending",
        }) as enqueue:
            response = self.client.post(
                "/admin/v1/bridge/players",
                headers={"X-Admin-Bridge-Key": "admin-read-test"},
                json={"operation_ref": "read-1"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["request_id"], "admin-get-players:read-1")
        enqueue.assert_called_once_with("read-1")

    def test_endpoint_rejects_extra_fields_before_queueing(self):
        with patch("app.enqueue_admin_players_read") as enqueue:
            response = self.client.post(
                "/admin/v1/bridge/players",
                headers={"X-Admin-Bridge-Key": "admin-read-test"},
                json={"operation_ref": "read-1", "job_type": "deposit"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "only operation_ref is allowed")
        enqueue.assert_not_called()

    def test_invalid_operation_ref_does_not_touch_database(self):
        with patch("app.db_enabled", return_value=True), patch("app.enqueue_bridge_job") as enqueue:
            result = enqueue_admin_players_read("bad ref")
        self.assertEqual(result, {"ok": False, "reason": "invalid_operation_ref"})
        enqueue.assert_not_called()

    def test_same_operation_ref_builds_same_idempotent_request_id(self):
        queued = {"job_id": "job-1", "request_id": "admin-get-players:read-1", "status": "pending"}
        with patch("app.db_enabled", return_value=True), patch("app.enqueue_bridge_job", return_value=queued) as enqueue:
            first = enqueue_admin_players_read("read-1")
            second = enqueue_admin_players_read("read-1")
        self.assertEqual(first, second)
        self.assertEqual(enqueue.call_args_list[0].args[0], "admin-get-players:read-1")
        self.assertEqual(enqueue.call_args_list[1].args[0], "admin-get-players:read-1")
        for call in enqueue.call_args_list:
            self.assertEqual(call.args[1], "get_players")
            self.assertEqual(call.args[2]["source"], "admin_readonly")


if __name__ == "__main__":
    unittest.main()
