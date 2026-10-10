import os
import unittest
from concurrent.futures import ThreadPoolExecutor
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from payment_schema import install_payment_schema, create_method_snapshot, reserve_withdrawal
from payment_calculations import calculate_quote_from_snapshot


class DynamicPaymentPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must not be used")
        cls.conn = psycopg2.connect(service="bridge_financial_test_local", cursor_factory=RealDictCursor)
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (telegram_id BIGINT PRIMARY KEY, balance BIGINT NOT NULL DEFAULT 0, reserved_balance BIGINT NOT NULL DEFAULT 0)")
            cur.execute("""CREATE TABLE IF NOT EXISTS cash_requests (
                id BIGSERIAL PRIMARY KEY, request_code TEXT UNIQUE NOT NULL, telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                request_type TEXT NOT NULL, amount_bot BIGINT NOT NULL DEFAULT 0, amount_cash BIGINT NOT NULL DEFAULT 0,
                method TEXT, status TEXT NOT NULL DEFAULT 'pending', details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("CREATE TABLE IF NOT EXISTS transactions (id BIGSERIAL PRIMARY KEY, telegram_id BIGINT, tx_type TEXT, amount BIGINT, status TEXT, details JSONB DEFAULT '{}'::jsonb)")
            install_payment_schema(cur)
            cur.execute("INSERT INTO users(telegram_id,balance,reserved_balance) VALUES(990001,50000,0) ON CONFLICT DO NOTHING")
            cur.execute("DELETE FROM cash_request_attachments WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=990001)")
            cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=990001)")
            cur.execute("DELETE FROM balance_reservations WHERE telegram_id=990001")
            cur.execute("DELETE FROM cash_requests WHERE telegram_id=990001")
            cur.execute("DELETE FROM payment_method_fields WHERE payment_method_direction_id IN (SELECT id FROM payment_method_directions WHERE payment_method_id IN (SELECT id FROM payment_methods WHERE method_key='stage5_test'))")
            cur.execute("DELETE FROM payment_method_directions WHERE payment_method_id IN (SELECT id FROM payment_methods WHERE method_key='stage5_test')")
            cur.execute("DELETE FROM payment_methods WHERE method_key='stage5_test'")
            cur.execute("UPDATE users SET reserved_balance=0 WHERE telegram_id=990001")
            cur.execute("INSERT INTO payment_methods(method_key,display_name) VALUES('stage5_test','Stage 5 Test') RETURNING id")
            cls.method_id = cur.fetchone()["id"]
            cur.execute("""INSERT INTO payment_method_directions(payment_method_id,direction,payment_currency,reference_currency,exchange_rate,rate_definition,conversion_multiplier,minimum_amount)
                           VALUES(%s,'withdraw','SYP','BOT',1,'one SYP per one unit',100,20000) RETURNING id""", (cls.method_id,))
            cls.direction_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO payment_method_fields(payment_method_direction_id,field_key,required_mode) VALUES(%s,'recipient_account','required')", (cls.direction_id,))
        cls.conn.commit()
        cls.conn.autocommit = False

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute("DELETE FROM cash_request_attachments WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=990001)")
            cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=990001)")
            cur.execute("DELETE FROM balance_reservations WHERE telegram_id=990001")
            cur.execute("DELETE FROM cash_requests WHERE telegram_id=990001")
            cur.execute("DELETE FROM payment_method_fields WHERE payment_method_direction_id=%s", (cls.direction_id,))
            cur.execute("DELETE FROM payment_method_directions WHERE id=%s", (cls.direction_id,))
            cur.execute("DELETE FROM payment_methods WHERE id=%s", (cls.method_id,))
        cls.conn.commit(); cls.conn.close()

    def setUp(self):
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM cash_request_attachments WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=990001)")
                cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id IN (SELECT id FROM cash_requests WHERE telegram_id=990001)")
                cur.execute("DELETE FROM balance_reservations WHERE telegram_id=990001")
                cur.execute("DELETE FROM cash_requests WHERE telegram_id=990001")
                cur.execute("UPDATE users SET balance=50000,reserved_balance=0 WHERE telegram_id=990001")

    def test_snapshot_and_reservation_are_created_with_real_postgres(self):
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot,amount_cash,method,status) VALUES('STAGE5-WD',990001,'withdraw',20000,200,'Stage 5 Test','pending') RETURNING id")
                request_id = cur.fetchone()["id"]
                snapshot_id = create_method_snapshot(cur, request_id, self.method_id, self.direction_id, None, Json({"conversion_multiplier":"100","minimum_amount":"20000","recipient_account":"snapshot-account"}))
                cur.execute("UPDATE cash_requests SET payment_method_id=%s,payment_direction='withdraw',method_snapshot_id=%s,reservation_id=NULL WHERE id=%s", (self.method_id, snapshot_id, request_id))
                reservation_id = reserve_withdrawal(cur, request_id, 990001, 20000)
                cur.execute("SELECT snapshot FROM cash_request_method_snapshots WHERE id=%s", (snapshot_id,))
                self.assertEqual(cur.fetchone()["snapshot"]["recipient_account"], "snapshot-account")
                cur.execute("SELECT status,reserved_balance FROM balance_reservations JOIN users ON users.telegram_id=balance_reservations.telegram_id WHERE id=%s", (reservation_id,))
                row = cur.fetchone(); self.assertEqual(row["status"], "held"); self.assertEqual(row["reserved_balance"], 20000)

    def test_atomic_rollback_leaves_no_orphan_snapshot(self):
        with self.assertRaises(RuntimeError):
            with self.conn:
                with self.conn.cursor() as cur:
                    cur.execute("INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot,amount_cash,method,status) VALUES('STAGE5-ROLLBACK',990001,'topup',100,1,'Stage 5 Test','pending') RETURNING id")
                    request_id = cur.fetchone()["id"]
                    create_method_snapshot(cur, request_id, self.method_id, self.direction_id, None, Json({"x":"y"}))
                    raise RuntimeError("forced_storage_failure")
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM cash_requests WHERE request_code='STAGE5-ROLLBACK'")
            self.assertEqual(cur.fetchone()["n"], 0)

    def test_snapshot_quote_is_immutable_after_method_changes(self):
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot,amount_cash,method,status) VALUES('STAGE5-SNAPSHOT',990001,'topup',10000,100,'Stage 5 Test','pending') RETURNING id")
                request_id = cur.fetchone()["id"]
                original = {"direction": "deposit", "exchange_rate": "1", "conversion_multiplier": "100", "minimum_amount": "1", "bonus_enabled": False, "fee_type": "none", "amount_scale": 0}
                snapshot_id = create_method_snapshot(cur, request_id, self.method_id, self.direction_id, None, Json(original))
                cur.execute("UPDATE payment_method_directions SET exchange_rate=9,conversion_multiplier=3,fee_type='percent',fee_value=50,bonus_enabled=TRUE,bonus_percent=20,minimum_amount=999 WHERE id=%s", (self.direction_id,))
                cur.execute("SELECT snapshot FROM cash_request_method_snapshots WHERE id=%s", (snapshot_id,))
                stored = cur.fetchone()["snapshot"]
                quote = calculate_quote_from_snapshot("100", stored, "deposit")
                self.assertEqual(quote["bot_amount"], 10000)
                self.assertEqual(stored["conversion_multiplier"], "100")
                cur.execute("DELETE FROM cash_request_method_snapshots WHERE cash_request_id=%s", (request_id,))
                cur.execute("DELETE FROM cash_requests WHERE id=%s", (request_id,))
                cur.execute("UPDATE payment_method_directions SET exchange_rate=1,conversion_multiplier=100,fee_type='none',fee_value=0,bonus_enabled=FALSE,bonus_percent=0,minimum_amount=20000 WHERE id=%s", (self.direction_id,))

    def test_concurrent_reservations_cannot_bypass_available_balance(self):
        with self.conn:
            with self.conn.cursor() as cur:
                cur.execute("UPDATE users SET balance=1000,reserved_balance=0 WHERE telegram_id=990001")
                ids = []
                for code in ("STAGE5-RACE-A", "STAGE5-RACE-B"):
                    cur.execute("INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot,amount_cash,method,status) VALUES(%s,990001,'withdraw',700,7,'Stage 5 Test','pending') RETURNING id", (code,))
                    ids.append(cur.fetchone()["id"])

        def attempt(request_id):
            conn = psycopg2.connect(service="bridge_financial_test_local", cursor_factory=RealDictCursor)
            try:
                with conn:
                    with conn.cursor() as cur:
                        try:
                            reserve_withdrawal(cur, request_id, 990001, 700)
                            return "ok"
                        except ValueError:
                            return "rejected"
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ids))
        with self.conn.cursor() as cur:
            cur.execute("SELECT reserved_balance FROM users WHERE telegram_id=990001")
            self.assertEqual(cur.fetchone()["reserved_balance"], 700)
            cur.execute("DELETE FROM balance_reservations WHERE cash_request_id IN (%s,%s)", ids)
            cur.execute("DELETE FROM cash_requests WHERE id IN (%s,%s)", ids)
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])


if __name__ == "__main__":
    unittest.main()
