import configparser
import json
import os
import threading
import unittest
import uuid
from urllib.parse import quote

os.environ.pop("DATABASE_URL", None)
import app


LOCAL_SERVICE = "bridge_financial_test_local"
LOCAL_USER = "bridge_test_runner"
LOCAL_DB = "bridge_financial_test"
LOCAL_CHAT_ID = 991234


def local_database_url():
    cfg = configparser.ConfigParser()
    cfg.read("/home/ubuntu/.pg_service.conf")
    section = cfg[LOCAL_SERVICE]
    if section.get("host") != "127.0.0.1" or section.get("port") != "5432":
        raise RuntimeError("test service is not local")
    if section.get("dbname") != LOCAL_DB or section.get("user") != LOCAL_USER:
        raise RuntimeError("test service identity mismatch")
    return (
        f"postgresql://{quote(section['user'])}:{quote(section['password'])}"
        f"@{section['host']}:{section['port']}/{section['dbname']}?sslmode=disable"
    )


class LegacyWithdrawalPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_database_url = app.DATABASE_URL
        cls._old_admin_bridge_shared_secret = app.ADMIN_BRIDGE_SHARED_SECRET
        app.DATABASE_URL = local_database_url()
        app.BRIDGE_SHARED_SECRET = "legacy-local-only"
        app._DB_POOL = None
        app._DB_INITIALIZED = False
        app.ensure_db()
        cls.client = app.app.test_client()
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO users(telegram_id,balance,ichancy_player_id,ichancy_currency)
                    VALUES(%s,50000,'legacy-player','USD')
                    ON CONFLICT(telegram_id) DO UPDATE SET
                        balance=50000,reserved_balance=0,ichancy_player_id='legacy-player',ichancy_currency='USD'
                """, (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM transactions WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_request_attachments WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_idempotency WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_audit_log WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_reconciliations WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM balance_reservations WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_requests WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM bridge_jobs WHERE payload->>'telegram_id'=%s", (str(LOCAL_CHAT_ID),))
                cur.execute("DELETE FROM bridge_devices WHERE device_id='legacy-test-device'")

    @classmethod
    def tearDownClass(cls):
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM transactions WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_request_attachments WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_idempotency WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_audit_log WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_reconciliations WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM balance_reservations WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_requests WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM bridge_jobs WHERE payload->>'telegram_id'=%s", (str(LOCAL_CHAT_ID),))
                cur.execute("DELETE FROM bridge_devices WHERE device_id='legacy-test-device'")
        if app._DB_POOL is not None:
            app._DB_POOL.closeall()
            app._DB_POOL = None
        app.DATABASE_URL = cls._old_database_url
        app.ADMIN_BRIDGE_SHARED_SECRET = cls._old_admin_bridge_shared_secret
        app._DB_INITIALIZED = False

    def _auth_headers(self):
        return {"X-Bridge-Key": "legacy-local-only", "Content-Type": "application/json"}

    def setUp(self):
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM transactions WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_request_attachments WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_idempotency WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_audit_log WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM financial_reconciliations WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=%s)", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM balance_reservations WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM cash_requests WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                cur.execute("DELETE FROM bridge_jobs WHERE payload->>'telegram_id'=%s", (str(LOCAL_CHAT_ID),))
                cur.execute("UPDATE users SET balance=50000,reserved_balance=0 WHERE telegram_id=%s", (LOCAL_CHAT_ID,))

    def _approve_withdrawal(self, cash_request_id):
        app.ADMIN_BRIDGE_SHARED_SECRET = "legacy-admin-local-only"
        response = self.client.post(
            f"/admin/v1/financial/cash-requests/{cash_request_id}/decision",
            headers={"X-Admin-Bridge-Key": "legacy-admin-local-only"},
            json={
                "decision": "approve",
                "reason": "local withdrawal approval",
                "idempotency_key": f"legacy-approve-{cash_request_id}",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())

    def _heartbeat(self):
        response = self.client.post(
            "/bridge/v1/heartbeat",
            headers=self._auth_headers(),
            json={"device_id": "legacy-test-device", "device_name": "local test"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())

    def _approved_withdrawal(self, operation_ref):
        queued = app.enqueue_ichancy_withdraw(LOCAL_CHAT_ID, 20000, operation_ref)
        self.assertTrue(queued["ok"], queued)
        self._heartbeat()
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM bridge_jobs WHERE job_id=%s", (queued["job_id"],))
                payload = cur.fetchone()["payload"]
        self._approve_withdrawal(payload["cash_request_id"])
        return queued, payload["cash_request_id"]

    def _claim(self, job_types=("withdraw",)):
        client = app.app.test_client()
        return client.post(
            "/bridge/v1/jobs/next",
            headers=self._auth_headers(),
            json={"device_id": "legacy-test-device", "job_types": list(job_types)},
        )

    def _complete(self, job_id, attempt, status, result=None, error=None):
        client = app.app.test_client()
        body = {
            "device_id": "legacy-test-device",
            "execution_attempt_id": attempt,
            "status": status,
            "result": result or {},
        }
        if error:
            body["error"] = error
        return client.post(
            f"/bridge/v1/jobs/{job_id}/complete",
            headers=self._auth_headers(),
            json=body,
        )

    def test_legacy_withdrawal_uses_reservation_and_guarded_settlement(self):
        queued = app.enqueue_ichancy_withdraw(LOCAL_CHAT_ID, 20000, "legacy-regression")
        self.assertTrue(queued["ok"])
        self._heartbeat()

        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status,payload FROM bridge_jobs WHERE job_id=%s", (queued["job_id"],))
                job_before = cur.fetchone()
                self.assertEqual(job_before["status"], "pending")
                payload = job_before["payload"]
                self.assertTrue(payload["cash_request_id"])
                cur.execute("SELECT status FROM cash_requests WHERE id=%s", (payload["cash_request_id"],))
                self.assertEqual(cur.fetchone()["status"], "funds_reserved")

        self._approve_withdrawal(payload["cash_request_id"])
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cash_requests WHERE id=%s", (payload["cash_request_id"],))
                self.assertEqual(cur.fetchone()["status"], "approved")

        claimed = self.client.post(
            "/bridge/v1/jobs/next",
            headers=self._auth_headers(),
            json={"device_id": "legacy-test-device", "job_types": ["withdraw"]},
        )
        self.assertEqual(claimed.status_code, 200, claimed.get_json())
        claimed_job = claimed.get_json()["job"]
        self.assertEqual(claimed_job["job_id"], queued["job_id"])
        attempt = claimed_job["execution_attempt_id"]

        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cash_requests WHERE id=%s", (payload["cash_request_id"],))
                self.assertEqual(cur.fetchone()["status"], "payout_in_progress")

        completed = self.client.post(
            f"/bridge/v1/jobs/{queued['job_id']}/complete",
            headers=self._auth_headers(),
            json={
                "device_id": "legacy-test-device",
                "execution_attempt_id": attempt,
                "status": "succeeded",
                "result": {"external_reference": "legacy-local-ref"},
            },
        )
        self.assertEqual(completed.status_code, 200, completed.get_json())

        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cash_requests WHERE id=%s", (payload["cash_request_id"],))
                self.assertEqual(cur.fetchone()["status"], "completed")
                cur.execute("SELECT status FROM balance_reservations WHERE cash_request_id=%s", (payload["cash_request_id"],))
                self.assertEqual(cur.fetchone()["status"], "settled")
                cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                self.assertEqual(dict(cur.fetchone()), {"balance": 30000, "reserved_balance": 0})

    def test_claim_before_admin_approval_is_rejected_without_claiming_job(self):
        queued = app.enqueue_ichancy_withdraw(LOCAL_CHAT_ID, 20000, "claim-before-approval")
        self.assertTrue(queued["ok"])
        self._heartbeat()

        response = self.client.post(
            "/bridge/v1/jobs/next",
            headers=self._auth_headers(),
            json={"device_id": "legacy-test-device", "job_types": ["withdraw"]},
        )
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error"], "withdrawal_admin_approval_required")
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM bridge_jobs WHERE job_id=%s", (queued["job_id"],))
                self.assertEqual(cur.fetchone()["status"], "pending")
                cur.execute("SELECT status FROM cash_requests WHERE details->>'job_id'=%s", (queued["job_id"],))
                self.assertEqual(cur.fetchone()["status"], "funds_reserved")
                cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                self.assertEqual(dict(cur.fetchone()), {"balance": 50000, "reserved_balance": 20000})

    def test_duplicate_completion_and_external_success_are_idempotent_over_http(self):
        queued, cash_request_id = self._approved_withdrawal("duplicate-completion")
        claimed = self._claim()
        self.assertEqual(claimed.status_code, 200, claimed.get_json())
        attempt = claimed.get_json()["job"]["execution_attempt_id"]
        body = {"external_reference": "duplicate-ref"}

        first = self._complete(queued["job_id"], attempt, "succeeded", result=body)
        second = self._complete(queued["job_id"], attempt, "succeeded", result=body)
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())

        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status,external_outcome FROM cash_requests WHERE id=%s", (cash_request_id,))
                self.assertEqual(dict(cur.fetchone()), {"status": "completed", "external_outcome": "external_confirmed_success"})
                cur.execute("SELECT status FROM balance_reservations WHERE cash_request_id=%s", (cash_request_id,))
                self.assertEqual(cur.fetchone()["status"], "settled")
                cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                self.assertEqual(dict(cur.fetchone()), {"balance": 30000, "reserved_balance": 0})
                cur.execute("SELECT COUNT(*) AS count FROM transactions WHERE details->>'job_id'=%s", (queued["job_id"],))
                self.assertEqual(cur.fetchone()["count"], 1)
                cur.execute("SELECT COUNT(*) AS count FROM financial_reconciliations WHERE cash_request_id=%s", (cash_request_id,))
                self.assertEqual(cur.fetchone()["count"], 0)

    def test_confirmed_failure_releases_once_and_blocks_payout_again(self):
        queued, cash_request_id = self._approved_withdrawal("confirmed-failure")
        claimed = self._claim()
        self.assertEqual(claimed.status_code, 200, claimed.get_json())
        attempt = claimed.get_json()["job"]["execution_attempt_id"]
        failed = self._complete(queued["job_id"], attempt, "failed", error="provider_rejected")
        repeated = self._complete(queued["job_id"], attempt, "failed", error="provider_rejected")
        self.assertEqual(failed.status_code, 200, failed.get_json())
        self.assertEqual(repeated.status_code, 200, repeated.get_json())
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status,external_outcome FROM cash_requests WHERE id=%s", (cash_request_id,))
                self.assertEqual(dict(cur.fetchone()), {"status": "failed", "external_outcome": "external_confirmed_failure"})
                cur.execute("SELECT status FROM balance_reservations WHERE cash_request_id=%s", (cash_request_id,))
                self.assertEqual(cur.fetchone()["status"], "released")
                cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                self.assertEqual(dict(cur.fetchone()), {"balance": 50000, "reserved_balance": 0})
        self.assertEqual(self._claim().get_json()["job"], None)

    def test_unknown_result_holds_reservation_and_blocks_normal_retry(self):
        queued, cash_request_id = self._approved_withdrawal("unknown-result")
        claimed = self._claim()
        self.assertEqual(claimed.status_code, 200, claimed.get_json())
        attempt = claimed.get_json()["job"]["execution_attempt_id"]
        unknown = self._complete(queued["job_id"], attempt, "unknown_external_result", error="timeout")
        self.assertEqual(unknown.status_code, 200, unknown.get_json())
        retry = self._claim()
        self.assertEqual(retry.status_code, 200, retry.get_json())
        self.assertIsNone(retry.get_json()["job"])
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status,external_outcome FROM cash_requests WHERE id=%s", (cash_request_id,))
                self.assertEqual(dict(cur.fetchone()), {"status": "unknown_external_result", "external_outcome": "unknown_external_result"})
                cur.execute("SELECT status FROM balance_reservations WHERE cash_request_id=%s", (cash_request_id,))
                self.assertEqual(cur.fetchone()["status"], "unknown_external_result")
                cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s", (LOCAL_CHAT_ID,))
                self.assertEqual(dict(cur.fetchone()), {"balance": 50000, "reserved_balance": 20000})
                cur.execute("SELECT COUNT(*) AS count FROM financial_reconciliations WHERE cash_request_id=%s", (cash_request_id,))
                self.assertEqual(cur.fetchone()["count"], 1)

    def test_concurrent_claim_has_one_payout_start(self):
        queued, cash_request_id = self._approved_withdrawal("concurrent-claim")
        barrier = threading.Barrier(2)
        responses = []
        errors = []

        def worker():
            try:
                barrier.wait()
                response = self._claim()
                responses.append((response.status_code, response.get_json()))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(responses), 2)
        successful_claims = [item for item in responses if item[0] == 200 and item[1].get("job")]
        self.assertEqual(len(successful_claims), 1, responses)
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM cash_requests WHERE id=%s", (cash_request_id,))
                self.assertEqual(cur.fetchone()["status"], "payout_in_progress")
                cur.execute("SELECT COUNT(*) AS count FROM transactions WHERE details->>'job_id'=%s", (queued["job_id"],))
                self.assertEqual(cur.fetchone()["count"], 1)

    def test_old_withdraw_job_without_cash_request_is_not_claimed_or_completed(self):
        old_job = str(uuid.uuid4())
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bridge_jobs(job_id,request_id,job_type,payload) VALUES(%s,%s,'withdraw',%s)",
                    (old_job, f"old-withdraw-{uuid.uuid4()}", json.dumps({"telegram_id": LOCAL_CHAT_ID, "bot_amount": 200})),
                )
        self._heartbeat()
        response = self.client.post(
            "/bridge/v1/jobs/next",
            headers=self._auth_headers(),
            json={"device_id": "legacy-test-device", "job_types": ["withdraw"]},
        )
        self.assertEqual(response.status_code, 409, response.get_json())
        with app.db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM bridge_jobs WHERE job_id=%s", (old_job,))
                self.assertEqual(cur.fetchone()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
