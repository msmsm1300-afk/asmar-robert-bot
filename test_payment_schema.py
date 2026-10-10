import os
import threading
import uuid
import unittest

import psycopg2
from psycopg2.extras import RealDictCursor, Json

from payment_schema import (
    install_payment_schema,
    reserve_withdrawal,
    release_withdrawal,
    mark_withdrawal_unknown,
    record_external_outcome,
    settle_withdrawal,
    approve_withdrawal,
    start_withdrawal_payout,
)


class LocalPaymentSchemaTests(unittest.TestCase):
    """Real PostgreSQL tests in an ephemeral schema on the local test service."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be unset for local-only tests")
        service_file = os.environ.get("PGSERVICEFILE", "/home/ubuntu/.pg_service.conf")
        service = os.environ.get("PGSERVICE", "bridge_financial_test_local")
        os.environ["PGSERVICEFILE"] = service_file
        cls.conn = psycopg2.connect(service=service, cursor_factory=RealDictCursor)
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, inet_server_addr(), inet_server_port()")
            row = cur.fetchone()
            if row["current_database"] != "bridge_financial_test" or row["current_user"] != "bridge_test_runner":
                raise RuntimeError("local test PostgreSQL identity mismatch")
            if str(row["inet_server_addr"]) not in {"127.0.0.1", "None"} or int(row["inet_server_port"] or 5432) != 5432:
                raise RuntimeError("local test PostgreSQL endpoint mismatch")
        cls.conn.autocommit = False
        cls.schema = "payment_test_" + uuid.uuid4().hex[:12]
        with cls.conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{cls.schema}"')
            cur.execute(f'SET search_path TO "{cls.schema}"')
            cur.execute("""
                CREATE TABLE users (
                    telegram_id BIGINT PRIMARY KEY,
                    balance BIGINT NOT NULL DEFAULT 0,
                    reserved_balance BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE cash_requests (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    request_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    amount_bot BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE transactions (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    tx_type TEXT NOT NULL,
                    amount BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb
                )
            """)
            install_payment_schema(cur)
            cur.execute("INSERT INTO users(telegram_id,balance) VALUES(101,1000)")
            cur.execute("INSERT INTO cash_requests(telegram_id,request_type,status,amount_bot) VALUES(101,'withdraw','pending',400) RETURNING id")
            cls.request_id = cur.fetchone()["id"]
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
        cls.conn.close()

    def setUp(self):
        with self.conn.cursor() as cur:
            cur.execute(f'SET search_path TO "{self.schema}"')
            cur.execute("UPDATE cash_requests SET status='pending',reservation_id=NULL,method_snapshot_id=NULL,payment_method_id=NULL")
            cur.execute("UPDATE transactions SET reservation_id=NULL")
            cur.execute("DELETE FROM financial_reconciliations")
            cur.execute("DELETE FROM balance_reservations")
            cur.execute("DELETE FROM cash_request_method_snapshots")
            cur.execute("DELETE FROM payment_method_fields")
            cur.execute("DELETE FROM payment_method_directions")
            cur.execute("DELETE FROM payment_assets")
            cur.execute("DELETE FROM payment_methods")
            cur.execute("DELETE FROM payment_method_audit")
            cur.execute("DELETE FROM cash_requests")
            cur.execute("DELETE FROM transactions")
            cur.execute("UPDATE users SET balance=1000,reserved_balance=0")
            cur.execute("INSERT INTO cash_requests(telegram_id,request_type,status,amount_bot) VALUES(101,'withdraw','pending',400) RETURNING id")
            self.request_id = cur.fetchone()["id"]
        self.conn.commit()

    def _base_method(self, cur):
        cur.execute("INSERT INTO payment_methods(method_key,display_name) VALUES('local_test','Local Test') RETURNING id")
        method_id = cur.fetchone()["id"]
        cur.execute("""
            INSERT INTO payment_method_directions(
                payment_method_id,direction,payment_currency,reference_currency,
                exchange_rate,rate_definition,minimum_amount
            ) VALUES(%s,'deposit','SYP','SYP',1,'1 SYP payment unit per 1 SYP reference unit',1)
            RETURNING id
        """, (method_id,))
        direction_id = cur.fetchone()["id"]
        return method_id, direction_id

    def test_schema_constraints_and_snapshot_are_additive(self):
        with self.conn.cursor() as cur:
            method_id, direction_id = self._base_method(cur)
            cur.execute("INSERT INTO payment_method_fields(payment_method_direction_id,field_key,required_mode) VALUES(%s,'proof_image','optional')", (direction_id,))
            cur.execute("""
                INSERT INTO cash_request_method_snapshots(cash_request_id,payment_method_id,payment_method_direction_id,snapshot)
                VALUES(%s,%s,%s,%s)
            """, (self.request_id, method_id, direction_id, Json({"rate": "1", "currency": "SYP"})))
            with self.assertRaises(psycopg2.errors.CheckViolation):
                cur.execute("""
                    INSERT INTO payment_method_directions(
                        payment_method_id,direction,payment_currency,reference_currency,
                        exchange_rate,rate_definition,minimum_amount
                    ) VALUES(%s,'withdraw','SYP','SYP',0,'invalid',1)
                """, (method_id,))
        self.conn.rollback()

    def test_reserve_is_idempotent_and_tracks_available_balance(self):
        with self.conn.cursor() as cur:
            reservation_id = reserve_withdrawal(cur, self.request_id, 101, 400)
            self.assertTrue(reservation_id)
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            self.assertEqual(dict(cur.fetchone()), {"balance": 1000, "reserved_balance": 400})
            with self.assertRaises(ValueError):
                reserve_withdrawal(cur, self.request_id, 101, 400)
        self.conn.commit()

    def test_release_returns_reserved_balance_only_for_confirmed_failure(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            record_external_outcome(cur, self.request_id, "external_confirmed_failure")
            self.assertTrue(release_withdrawal(cur, self.request_id, "rejected"))
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            self.assertEqual(dict(cur.fetchone()), {"balance": 1000, "reserved_balance": 0})
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["status"], "rejected")
        self.conn.commit()

    def test_explicit_pending_to_reserved_to_approved_to_payout_path(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            self.assertTrue(approve_withdrawal(cur, self.request_id))
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["status"], "approved")
            self.assertTrue(start_withdrawal_payout(cur, self.request_id))
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["status"], "payout_in_progress")
        self.conn.commit()

    def test_approval_and_payout_require_reserved_withdrawal(self):
        with self.assertRaises(ValueError):
            with self.conn.cursor() as cur:
                approve_withdrawal(cur, self.request_id)
        self.conn.rollback()
        with self.assertRaises(ValueError):
            with self.conn.cursor() as cur:
                start_withdrawal_payout(cur, self.request_id)
        self.conn.rollback()

    def test_direct_active_status_without_reservation_is_rejected_at_commit(self):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE cash_requests SET status='approved' WHERE id=%s", (self.request_id,))
        with self.assertRaises(psycopg2.errors.CheckViolation):
            self.conn.commit()
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT status,reservation_id FROM cash_requests WHERE id=%s", (self.request_id,))
            row = cur.fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["reservation_id"])

    def test_direct_payout_and_completed_without_reservation_are_rejected(self):
        for status in ("payout_in_progress", "completed"):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE cash_requests SET status=%s WHERE id=%s", (status, self.request_id))
            with self.assertRaises(psycopg2.errors.CheckViolation):
                self.conn.commit()
            self.conn.rollback()

    def test_transition_rollback_restores_pending_without_reservation(self):
        with self.assertRaises(RuntimeError):
            with self.conn.cursor() as cur:
                reserve_withdrawal(cur, self.request_id, 101, 400)
                approve_withdrawal(cur, self.request_id)
                raise RuntimeError("simulated_transition_failure")
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT status,reservation_id FROM cash_requests WHERE id=%s", (self.request_id,))
            row = cur.fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertIsNone(row["reservation_id"])
            cur.execute("SELECT COUNT(*) AS c FROM balance_reservations")
            self.assertEqual(cur.fetchone()["c"], 0)

    def test_non_financial_completed_request_remains_compatible(self):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE cash_requests SET request_type='topup',status='completed' WHERE id=%s", (self.request_id,))
        self.conn.commit()

    def test_unknown_preserves_reservation_and_creates_reconciliation(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            mark_withdrawal_unknown(cur, self.request_id, "network interruption")
            self.assertFalse(mark_withdrawal_unknown(cur, self.request_id, "network interruption"))
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            self.assertEqual(dict(cur.fetchone()), {"balance": 1000, "reserved_balance": 400})
            cur.execute("SELECT status FROM balance_reservations WHERE cash_request_id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["status"], "unknown_external_result")
            cur.execute("SELECT COUNT(*) AS count FROM financial_reconciliations WHERE cash_request_id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["count"], 1)
            with self.assertRaises(ValueError):
                release_withdrawal(cur, self.request_id, "failed")
        self.conn.commit()

    def test_different_reconciliation_keys_are_not_merged(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            for key, reason in (("case-a", "first case"), ("case-b", "second case")):
                cur.execute("""
                    INSERT INTO financial_reconciliations
                        (cash_request_id,reservation_id,reconciliation_key,external_status,local_status,reason)
                    SELECT %s,id,%s,'unknown','unknown_external_result',%s
                    FROM balance_reservations WHERE cash_request_id=%s
                """, (self.request_id, key, reason, self.request_id))
            cur.execute("SELECT COUNT(*) AS count FROM financial_reconciliations WHERE cash_request_id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["count"], 2)
        self.conn.commit()

    def test_concurrent_unknown_reconciliation_is_inserted_once(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
        self.conn.commit()
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            conn = psycopg2.connect(service=os.environ.get("PGSERVICE", "bridge_financial_test_local"), cursor_factory=RealDictCursor)
            try:
                with conn.cursor() as cur:
                    cur.execute(f'SET search_path TO "{self.schema}"')
                    barrier.wait()
                    results.append(mark_withdrawal_unknown(cur, self.request_id, "concurrent timeout"))
                conn.commit()
            except Exception as exc:
                errors.append(exc)
                conn.rollback()
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM financial_reconciliations WHERE cash_request_id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["count"], 1)
        self.conn.commit()

    def test_settle_is_once_only_and_rolls_back_on_failure(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            with self.assertRaises(ValueError):
                settle_withdrawal(cur, self.request_id, "local-ref-1")
            record_external_outcome(cur, self.request_id, "external_confirmed_success", "local-ref-1")
            self.assertTrue(settle_withdrawal(cur, self.request_id, "local-ref-1"))
            self.assertFalse(settle_withdrawal(cur, self.request_id, "local-ref-1"))
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            self.assertEqual(dict(cur.fetchone()), {"balance": 600, "reserved_balance": 0})
        self.conn.commit()

    def test_late_confirmed_success_settles_unknown_without_replay(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            mark_withdrawal_unknown(cur, self.request_id, "timeout")
            record_external_outcome(cur, self.request_id, "external_confirmed_success", "late-ref")
            self.assertTrue(settle_withdrawal(cur, self.request_id, "late-ref"))
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            self.assertEqual(dict(cur.fetchone()), {"balance": 600, "reserved_balance": 0})
        self.conn.commit()

    def test_completed_request_requires_confirmed_success(self):
        with self.conn.cursor() as cur:
            reserve_withdrawal(cur, self.request_id, 101, 400)
            with self.assertRaises(ValueError):
                settle_withdrawal(cur, self.request_id)
            cur.execute("SELECT status FROM cash_requests WHERE id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["status"], "funds_reserved")
        self.conn.rollback()

    def test_transaction_rollback_restores_reservation_and_balance(self):
        with self.assertRaises(RuntimeError):
            with self.conn.cursor() as cur:
                reserve_withdrawal(cur, self.request_id, 101, 400)
                cur.execute("UPDATE users SET reserved_balance=0 WHERE telegram_id=101")
                raise RuntimeError("simulated_failure")
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=101")
            self.assertEqual(dict(cur.fetchone()), {"balance": 1000, "reserved_balance": 0})
            cur.execute("SELECT COUNT(*) AS c FROM balance_reservations")
            self.assertEqual(cur.fetchone()["c"], 0)


if __name__ == "__main__":
    unittest.main()
