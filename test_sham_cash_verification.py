import contextlib
import os
import threading
import unittest
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor

import app
from payment_schema import install_payment_schema, record_external_verification, settle_topup_after_verification


class ShamCashVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be unset")
        cls.conn = psycopg2.connect(service="bridge_financial_test_local", cursor_factory=RealDictCursor)
        cls.conn.autocommit = False
        with cls.conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_port()")
            row = cur.fetchone()
            if (row["current_database"], row["current_user"], int(row["inet_server_port"])) != ("bridge_financial_test", "bridge_test_runner", 5432):
                raise RuntimeError("wrong local test database")
            cls.schema = "phase85_sham_" + uuid.uuid4().hex[:10]
            cur.execute(f'CREATE SCHEMA "{cls.schema}"')
            cur.execute(f'SET search_path TO "{cls.schema}"')
            cur.execute("CREATE TABLE users (telegram_id BIGINT PRIMARY KEY, balance BIGINT NOT NULL DEFAULT 0, reserved_balance BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cur.execute("""CREATE TABLE cash_requests (
                id BIGSERIAL PRIMARY KEY, request_code TEXT UNIQUE NOT NULL,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id), request_type TEXT NOT NULL,
                amount_bot BIGINT NOT NULL DEFAULT 0, amount_cash BIGINT NOT NULL DEFAULT 0,
                method TEXT, destination TEXT, status TEXT NOT NULL DEFAULT 'pending',
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""CREATE TABLE transactions (
                id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                tx_code TEXT, tx_type TEXT NOT NULL, amount BIGINT NOT NULL, status TEXT NOT NULL,
                method TEXT, details JSONB NOT NULL DEFAULT '{}'::jsonb,
                balance_before BIGINT, balance_after BIGINT)""")
            cur.execute("CREATE TABLE bonuses (method TEXT PRIMARY KEY, percent INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cur.execute("INSERT INTO bonuses(method,percent) VALUES('sham',0)")
            install_payment_schema(cur)
            cur.execute("INSERT INTO users(telegram_id,balance) VALUES(850001,1000)")
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
        cls.conn.close()

    def setUp(self):
        self.old_db_enabled = app.db_enabled
        self.old_ensure_db = app.ensure_db
        self.old_db_conn = app.db_conn
        self.old_local_test_mode = os.environ.get("LOCAL_TEST_MODE")
        self.old_admin_secret = app.ADMIN_BRIDGE_SHARED_SECRET
        app.db_enabled = lambda: True
        app.ensure_db = lambda: None
        app.ADMIN_BRIDGE_SHARED_SECRET = "phase85-test-secret"
        app._BONUS_CACHE = {"expires": 0.0, "value": {}}
        os.environ["LOCAL_TEST_MODE"] = "1"

        @contextlib.contextmanager
        def local_db_conn():
            with self.conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{self.schema}"')
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        app.db_conn = local_db_conn
        with self.conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{self.schema}"')
            cur.execute("DELETE FROM financial_audit_log")
            cur.execute("DELETE FROM cash_request_external_verifications")
            cur.execute("DELETE FROM financial_idempotency")
            cur.execute("DELETE FROM transactions")
            cur.execute("DELETE FROM cash_requests")
            cur.execute("UPDATE users SET balance=1000,reserved_balance=0 WHERE telegram_id=850001")
        self.conn.commit()

    def tearDown(self):
        app.db_enabled = self.old_db_enabled
        app.ensure_db = self.old_ensure_db
        app.db_conn = self.old_db_conn
        app.ADMIN_BRIDGE_SHARED_SECRET = self.old_admin_secret
        if self.old_local_test_mode is None:
            os.environ.pop("LOCAL_TEST_MODE", None)
        else:
            os.environ["LOCAL_TEST_MODE"] = self.old_local_test_mode

    def _request(self, reference="SHAM-85-1"):
        result = app.create_sham_syp_topup_request(850001, 200, reference)
        self.assertTrue(result["ok"])
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM cash_requests WHERE request_code=%s", (result["request_code"],))
            return result, int(cur.fetchone()["id"])

    def test_create_and_admin_approval_do_not_credit(self):
        result, request_id = self._request()
        response = app.app.test_client().post(
            f"/admin/v1/financial/cash-requests/{request_id}/decision",
            headers={"X-Admin-Bridge-Key": "phase85-test-secret"},
            json={"decision": "approve", "reason": "reviewed", "idempotency_key": "phase85-approve-1"},
        )
        self.assertEqual(response.status_code, 200)
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=850001")
            self.assertEqual(cur.fetchone()["balance"], 1000)
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (request_id,))
            self.assertEqual(cur.fetchone()["status"], "approved")
            cur.execute("SELECT status FROM transactions WHERE cash_request_id=%s", (request_id,))
            self.assertEqual(cur.fetchone()["status"], "pending")

    def test_mock_is_disabled_without_explicit_local_flag(self):
        os.environ.pop("LOCAL_TEST_MODE", None)
        with self.assertRaisesRegex(RuntimeError, "provider_not_configured"):
            app.sham_cash_verification_provider().verify("REF", 200, "SYP", app.SHAM_SYP_ACCOUNT_ID)

    def test_verified_matching_result_settles_once(self):
        _, request_id = self._request()
        result = app.verify_and_settle_sham_syp_for_local_test(
            request_id, "EXT-85-1", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID,
        )
        self.assertEqual(result, {"verified": True, "settled": True, "test_only": True})
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=850001")
            self.assertEqual(cur.fetchone()["balance"], 21000)
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (request_id,))
            self.assertEqual(cur.fetchone()["status"], "completed")
            cur.execute("SELECT COUNT(*) AS n FROM transactions WHERE cash_request_id=%s AND tx_type='bot_topup'", (request_id,))
            self.assertEqual(cur.fetchone()["n"], 1)
            cur.execute("SELECT COUNT(*) AS n FROM cash_request_external_verifications WHERE cash_request_id=%s", (request_id,))
            self.assertEqual(cur.fetchone()["n"], 1)

    def test_repeated_verification_and_settlement_do_not_credit_twice(self):
        _, request_id = self._request("SHAM-85-2")
        app.verify_and_settle_sham_syp_for_local_test(request_id, "EXT-85-2", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID)
        with self.assertRaisesRegex(ValueError, "topup_not_verifiable"):
            app.verify_and_settle_sham_syp_for_local_test(request_id, "EXT-85-2", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID)
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=850001")
            self.assertEqual(cur.fetchone()["balance"], 21000)

    def test_mismatched_amount_currency_or_beneficiary_rejected(self):
        _, request_id = self._request("SHAM-85-3")
        with self.assertRaisesRegex(ValueError, "external_verification_mismatch"):
            app.verify_and_settle_sham_syp_for_local_test(request_id, "EXT-85-3", "201", "SYP", app.SHAM_SYP_ACCOUNT_ID)
        with self.assertRaisesRegex(ValueError, "external_verification_mismatch"):
            app.verify_and_settle_sham_syp_for_local_test(request_id, "EXT-85-4", "200", "USD", app.SHAM_SYP_ACCOUNT_ID)
        with self.assertRaisesRegex(ValueError, "external_verification_mismatch"):
            app.verify_and_settle_sham_syp_for_local_test(request_id, "EXT-85-5", "200", "SYP", "wrong-beneficiary")

    def test_manual_confirmation_endpoint_remains_rejected(self):
        _, request_id = self._request("SHAM-85-4")
        response = app.app.test_client().post(
            f"/admin/v1/financial/cash-requests/{request_id}/external-outcome",
            headers={"X-Admin-Bridge-Key": "phase85-test-secret"},
            json={"outcome": "external_confirmed_success", "external_reference": "FAKE"},
        )
        self.assertEqual(response.status_code, 409)
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=850001")
            self.assertEqual(cur.fetchone()["balance"], 1000)

    def test_failed_provider_result_cannot_settle(self):
        _, request_id = self._request("SHAM-85-5")
        with self.conn.cursor() as cur:
            cur.execute("UPDATE cash_requests SET status='approved' WHERE id=%s", (request_id,))
            record_external_verification(cur, request_id, "provider-test", "failed", "EXT-85-6", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID)
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute("UPDATE cash_requests SET status='externally_verified' WHERE id=%s", (request_id,))
        self.conn.commit()
        with self.conn.cursor() as cur:
            with self.assertRaisesRegex(ValueError, "verified_external_result_required"):
                settle_topup_after_verification(cur, request_id, "provider-test", "EXT-85-6", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID, "test")

    def test_mismatch_rolls_back_verification_status_and_balance(self):
        _, request_id = self._request("SHAM-85-6")
        with self.assertRaisesRegex(ValueError, "external_verification_mismatch"):
            app.verify_and_settle_sham_syp_for_local_test(request_id, "EXT-85-7", "201", "SYP", app.SHAM_SYP_ACCOUNT_ID)
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=850001")
            self.assertEqual(cur.fetchone()["balance"], 1000)
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (request_id,))
            self.assertEqual(cur.fetchone()["status"], "pending")
            cur.execute("SELECT COUNT(*) AS n FROM cash_request_external_verifications WHERE cash_request_id=%s", (request_id,))
            self.assertEqual(cur.fetchone()["n"], 0)

    def test_concurrent_settlement_credits_once(self):
        _, request_id = self._request("SHAM-85-7")
        with self.conn.cursor() as cur:
            cur.execute("UPDATE cash_requests SET status='externally_verified' WHERE id=%s", (request_id,))
            record_external_verification(cur, request_id, "provider-test", "verified", "EXT-85-8", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID)
        self.conn.commit()
        outcomes = []
        lock = threading.Lock()

        def attempt():
            try:
                conn = psycopg2.connect(service="bridge_financial_test_local", cursor_factory=RealDictCursor)
                try:
                    with conn:
                        with conn.cursor() as cur:
                            cur.execute(f'SET search_path TO "{self.schema}"')
                            result = settle_topup_after_verification(cur, request_id, "provider-test", "EXT-85-8", "200", "SYP", app.SHAM_SYP_ACCOUNT_ID, "test")
                            value = "settled" if result else "replayed"
                finally:
                    conn.close()
            except Exception as exc:
                value = type(exc).__name__
            with lock:
                outcomes.append(value)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes).count("settled"), 1)
        self.assertEqual(len(outcomes), 2)
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=850001")
            self.assertEqual(cur.fetchone()["balance"], 21000)


if __name__ == "__main__":
    unittest.main()
