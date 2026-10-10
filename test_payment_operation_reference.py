import os
import threading
import unittest
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from payment_calculations import normalize_operation_reference


class PaymentOperationReferencePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be unset for local-only tests")
        cls.conn = psycopg2.connect(service="bridge_financial_test_local", cursor_factory=RealDictCursor)
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_addr(), inet_server_port()")
            row = cur.fetchone()
            if (row["current_database"], row["current_user"]) != ("bridge_financial_test", "bridge_test_runner"):
                raise RuntimeError("local test PostgreSQL identity mismatch")
            if str(row["inet_server_addr"]) not in {"127.0.0.1", "None"} or int(row["inet_server_port"] or 5432) != 5432:
                raise RuntimeError("local test PostgreSQL endpoint mismatch")
            cls.schema = "phase78_ref_" + uuid.uuid4().hex[:10]
            cur.execute(f'CREATE SCHEMA "{cls.schema}"')
            cur.execute(f'SET search_path TO "{cls.schema}"')
            cur.execute("CREATE TABLE users (telegram_id BIGINT PRIMARY KEY, balance BIGINT NOT NULL DEFAULT 0, reserved_balance BIGINT NOT NULL DEFAULT 0)")
            cur.execute("CREATE TABLE cash_requests (id BIGSERIAL PRIMARY KEY, telegram_id BIGINT, request_type TEXT, status TEXT, method TEXT, payment_method_id BIGINT, payment_direction TEXT, details JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW())")
            cur.execute("CREATE TABLE transactions (id BIGSERIAL PRIMARY KEY, telegram_id BIGINT, tx_type TEXT, amount BIGINT, status TEXT, details JSONB NOT NULL DEFAULT '{}'::jsonb)")
            cur.execute("""
                CREATE UNIQUE INDEX operation_reference_once
                ON cash_requests (
                    COALESCE(payment_method_id::text, method),
                    COALESCE(payment_direction, 'deposit'),
                    lower(btrim(details->>'operation_ref'))
                )
                WHERE request_type='topup'
                  AND NULLIF(btrim(details->>'operation_ref'), '') IS NOT NULL
            """)
            cur.execute("INSERT INTO users(telegram_id,balance,reserved_balance) VALUES(101,500,0)")
        cls.conn.autocommit = False

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
        cls.conn.close()

    def setUp(self):
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute(f'SET search_path TO "{self.schema}"')
                cur.execute("DELETE FROM cash_requests")
                cur.execute("DELETE FROM transactions")
                cur.execute("UPDATE users SET balance=500,reserved_balance=0 WHERE telegram_id=101")

    def _insert(self, ref, method="Wish Money", direction="deposit", method_id=None):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO cash_requests(telegram_id,request_type,status,method,payment_method_id,payment_direction,details) VALUES(101,'topup','pending',%s,%s,%s,%s) RETURNING id", (method, method_id, direction, Json({"operation_ref": normalize_operation_reference(ref)})))
            return cur.fetchone()["id"]

    def test_new_reference_accepted_and_same_scope_rejected(self):
        self._insert(" TRX 123 ")
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            self._insert("trx   123")
        self.conn.rollback()

    def test_same_reference_is_allowed_for_different_method_and_direction(self):
        self._insert("ABC-1", method="Wish Money")
        self._insert("abc-1", method="Sham Cash")
        self._insert("abc-1", method="Wish Money", direction="withdraw")

    def test_concurrent_same_reference_allows_one_only(self):
        barrier = threading.Barrier(2)
        results = []

        def attempt():
            conn = psycopg2.connect(service="bridge_financial_test_local", cursor_factory=RealDictCursor)
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(f'SET search_path TO "{self.schema}"')
                        barrier.wait()
                        try:
                            cur.execute("INSERT INTO cash_requests(telegram_id,request_type,status,method,payment_direction,details) VALUES(101,'topup','pending','Concurrent','deposit',%s)", (Json({"operation_ref": "race-1"}),))
                            results.append("accepted")
                        except psycopg2.errors.UniqueViolation:
                            results.append("duplicate")
                            conn.rollback()
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["accepted", "duplicate"])

    def test_duplicate_does_not_change_balance_or_create_transaction(self):
        self._insert("funds-1")
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            self._insert("FUNDS-1")
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            row = cur.fetchone()
            self.assertEqual(row["balance"], 500)
            self.assertEqual(row["reserved_balance"], 0)
            cur.execute("SELECT COUNT(*) AS n FROM transactions WHERE telegram_id=101")
            self.assertEqual(cur.fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()
