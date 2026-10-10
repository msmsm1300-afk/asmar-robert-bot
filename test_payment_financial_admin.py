import contextlib
import os
import unittest
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor

import app
from payment_schema import install_payment_schema


class FinancialAdminContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be unset")
        cls.conn = psycopg2.connect(
            service=os.environ.get("PGSERVICE", "bridge_financial_test_local"),
            cursor_factory=RealDictCursor,
        )
        cls.conn.autocommit = False
        with cls.conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_port()")
            row = cur.fetchone()
            if row["current_database"] != "bridge_financial_test" or row["current_user"] != "bridge_test_runner":
                raise RuntimeError("wrong local test database")
            cls.schema = "financial_admin_" + uuid.uuid4().hex[:10]
            cur.execute(f'CREATE SCHEMA "{cls.schema}"')
            cur.execute(f'SET search_path TO "{cls.schema}"')
            cur.execute("CREATE TABLE users (telegram_id BIGINT PRIMARY KEY, balance BIGINT NOT NULL DEFAULT 0, reserved_balance BIGINT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
            cur.execute("""CREATE TABLE cash_requests (
                id BIGSERIAL PRIMARY KEY, request_code TEXT UNIQUE NOT NULL,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id), request_type TEXT NOT NULL,
                amount_bot BIGINT NOT NULL DEFAULT 0, amount_cash BIGINT NOT NULL DEFAULT 0,
                method TEXT, status TEXT NOT NULL DEFAULT 'pending', details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE TABLE transactions (id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id), tx_code TEXT, tx_type TEXT NOT NULL, amount BIGINT NOT NULL, status TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}'::jsonb, balance_before BIGINT, balance_after BIGINT)")
            install_payment_schema(cur)
            cur.execute("INSERT INTO users(telegram_id,balance) VALUES(901,1000)")
            cur.execute("INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot) VALUES('LOCAL-TOPUP',901,'topup',100) RETURNING id")
            cls.topup_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot) VALUES('LOCAL-WITHDRAW',901,'withdraw',400) RETURNING id")
            cls.withdraw_id = cur.fetchone()["id"]
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
        cls.conn.commit()
        cls.conn.close()

    def setUp(self):
        self.old_db_enabled = app.db_enabled
        self.old_ensure_db = app.ensure_db
        self.old_db_conn = app.db_conn
        self.old_admin_bridge_shared_secret = app.ADMIN_BRIDGE_SHARED_SECRET
        self.old_financial_admin_service_id = app.FINANCIAL_ADMIN_SERVICE_ID
        app.db_enabled = lambda: True
        app.ensure_db = lambda: None

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
        app.ADMIN_BRIDGE_SHARED_SECRET = "financial-admin-test"
        app.FINANCIAL_ADMIN_SERVICE_ID = "test-admin-service"

    def tearDown(self):
        app.db_enabled = self.old_db_enabled
        app.ensure_db = self.old_ensure_db
        app.db_conn = self.old_db_conn
        app.ADMIN_BRIDGE_SHARED_SECRET = self.old_admin_bridge_shared_secret
        app.FINANCIAL_ADMIN_SERVICE_ID = self.old_financial_admin_service_id
        with self.conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{self.schema}"')
            cur.execute("DELETE FROM financial_audit_log")
            cur.execute("DELETE FROM financial_idempotency")
            cur.execute("UPDATE cash_requests SET status='pending', reservation_id=NULL WHERE id IN (%s,%s)", (self.topup_id, self.withdraw_id))
            cur.execute("UPDATE users SET balance=1000,reserved_balance=0 WHERE telegram_id=901")
        self.conn.commit()

    def _post(self, request_id, decision, key, reason="reviewed"):
        return app.app.test_client().post(
            f"/admin/v1/financial/cash-requests/{request_id}/decision",
            headers={"X-Admin-Bridge-Key": "financial-admin-test"},
            json={"decision": decision, "reason": reason, "idempotency_key": key},
        )

    def test_topup_approval_is_audited_and_does_not_credit_balance(self):
        response = self._post(self.topup_id, "approve", "topup-approve-001")
        self.assertEqual(response.status_code, 200)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (self.topup_id,))
            self.assertEqual(cur.fetchone()["status"], "approved")
            cur.execute("SELECT balance FROM users WHERE telegram_id=901")
            self.assertEqual(cur.fetchone()["balance"], 1000)
            cur.execute("SELECT actor_id,actor_type,decision FROM financial_audit_log WHERE cash_request_id=%s", (self.topup_id,))
            self.assertEqual(dict(cur.fetchone()), {"actor_id": "test-admin-service", "actor_type": "service_account", "decision": "approve"})

    def test_same_idempotency_key_replays_without_second_audit(self):
        first = self._post(self.topup_id, "approve", "topup-replay-001")
        second = self._post(self.topup_id, "approve", "topup-replay-001")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM financial_audit_log WHERE cash_request_id=%s", (self.topup_id,))
            self.assertEqual(cur.fetchone()["count"], 1)

    def test_same_key_with_changed_content_is_rejected(self):
        self.assertEqual(self._post(self.topup_id, "approve", "topup-mismatch-001").status_code, 200)
        self.assertEqual(self._post(self.topup_id, "reject", "topup-mismatch-001").status_code, 409)

    def test_manual_confirm_and_external_outcome_are_not_accepted(self):
        response = self._post(self.topup_id, "confirm_deposit", "topup-confirm-001")
        self.assertEqual(response.status_code, 409)
        response = app.app.test_client().post(
            f"/admin/v1/financial/cash-requests/{self.topup_id}/external-outcome",
            headers={"X-Admin-Bridge-Key": "financial-admin-test"}, json={"outcome": "external_confirmed_success"},
        )
        self.assertEqual(response.status_code, 409)

    def test_withdraw_approval_requires_reservation(self):
        response = self._post(self.withdraw_id, "approve", "withdraw-approve-001")
        self.assertEqual(response.status_code, 409)
        with self.conn.cursor() as cur:
            cur.execute("SELECT status,balance,reserved_balance FROM cash_requests JOIN users ON users.telegram_id=cash_requests.telegram_id WHERE cash_requests.id=%s", (self.withdraw_id,))
            row = cur.fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["balance"], 1000)
            self.assertEqual(row["reserved_balance"], 0)


if __name__ == "__main__":
    unittest.main()
