import io
import os
import unittest
import uuid
from unittest.mock import patch

from PIL import Image

from payment_storage import (
    InMemoryObjectStorage,
    mark_asset_deleted,
    retire_asset,
    upload_payment_asset,
    validate_image,
)


def image_bytes(fmt, color=(20, 80, 140)):
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(stream, format=fmt)
    return stream.getvalue()


class PaymentStorageUnitTests(unittest.TestCase):
    def test_allowed_formats_and_real_mime(self):
        for fmt in ("JPEG", "PNG", "WEBP"):
            result = validate_image(image_bytes(fmt))
            self.assertEqual(result.content_type, f"image/{fmt.lower() if fmt != 'JPEG' else 'jpeg'}")
            self.assertEqual(len(result.sha256), 64)
        with self.assertRaises(ValueError):
            validate_image(b"not-an-image")

    def test_mime_mismatch_and_size_limit(self):
        png = image_bytes("PNG")
        with patch("payment_storage.MAX_ASSET_BYTES", len(png) - 1):
            with self.assertRaises(ValueError):
                validate_image(png)

    def test_upload_uses_private_mock_storage(self):
        storage = InMemoryObjectStorage()
        storage.upload("x", b"bytes", "image/png")
        self.assertTrue(storage.presign("x").startswith("memory://private/"))
        self.assertNotIn("AWS_ACCESS_KEY", repr(storage.objects))


@unittest.skipUnless(os.environ.get("PGSERVICE"), "requires explicit local PostgreSQL service")
class PaymentStoragePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("DATABASE_URL"):
            raise RuntimeError("DATABASE_URL must be unset")
        import psycopg2
        from psycopg2.extras import RealDictCursor
        import sys
        sys.path.insert(0, "/home/ubuntu/review-bot")
        from payment_schema import install_payment_schema
        cls.conn = psycopg2.connect(service=os.environ["PGSERVICE"], cursor_factory=RealDictCursor)
        cls.conn.autocommit = True
        cls.schema = "asset_test_" + uuid.uuid4().hex[:10]
        with cls.conn.cursor() as cur:
            cur.execute("SELECT current_database(),current_user")
            identity = cur.fetchone()
            if identity["current_database"] != "bridge_financial_test" or identity["current_user"] != "bridge_test_runner":
                raise RuntimeError("non-local database identity")
            cur.execute(f'CREATE SCHEMA "{cls.schema}"')
            cur.execute(f'SET search_path TO "{cls.schema}"')
            cur.execute("CREATE TABLE users(telegram_id BIGINT PRIMARY KEY)")
            cur.execute("CREATE TABLE cash_requests(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT REFERENCES users(telegram_id),request_type TEXT,status TEXT)")
            cur.execute("CREATE TABLE transactions(id BIGSERIAL PRIMARY KEY,telegram_id BIGINT,tx_type TEXT,amount BIGINT,status TEXT,details JSONB NOT NULL DEFAULT '{}'::jsonb)")
            install_payment_schema(cur)
            cur.execute("INSERT INTO users VALUES(55)")
            cur.execute("INSERT INTO payment_methods(method_key,display_name) VALUES('asset_method','Asset Method') RETURNING id")
            cls.method_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO cash_requests(telegram_id,request_type,status) VALUES(55,'topup','pending') RETURNING id")
            cls.request_id = cur.fetchone()["id"]

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE')
        cls.conn.close()

    def test_upload_replace_and_old_snapshot_is_retained(self):
        storage = InMemoryObjectStorage()
        with self.conn.cursor() as cur:
            old_id, old_key, old = upload_payment_asset(cur, storage, self.method_id, image_bytes("PNG"))
            cur.execute("INSERT INTO cash_request_method_snapshots(cash_request_id,payment_method_id,asset_id,snapshot) VALUES(%s,%s,%s,'{}')", (self.request_id, self.method_id, old_id))
            new_id, new_key, new = upload_payment_asset(cur, storage, self.method_id, image_bytes("JPEG"))
            result = retire_asset(cur, storage, old_id)
            self.assertEqual(result["status"], "retained")
            self.assertIn(old_key, storage.objects)
            self.assertNotEqual(old.sha256, new.sha256)
            cur.execute("SELECT asset_id FROM cash_request_method_snapshots WHERE cash_request_id=%s", (self.request_id,))
            self.assertEqual(cur.fetchone()["asset_id"], old_id)
            self.assertEqual(new.content_type, "image/jpeg")
        self.conn.rollback()

    def test_unreferenced_asset_can_be_marked_deleted_without_physical_delete(self):
        storage = InMemoryObjectStorage()
        with self.conn.cursor() as cur:
            asset_id, key, _ = upload_payment_asset(cur, storage, self.method_id, image_bytes("WEBP"))
            result = mark_asset_deleted(cur, storage, asset_id)
            self.assertEqual(result["status"], "deleted")
            self.assertIn(key, storage.objects)
        self.conn.rollback()

    def test_storage_failure_leaves_no_asset_row(self):
        class FailingStorage:
            def upload(self, key, content, content_type):
                raise RuntimeError("mock_storage_down")
            def delete(self, key):
                pass
        with self.conn.cursor() as cur:
            with self.assertRaises(RuntimeError):
                upload_payment_asset(cur, FailingStorage(), self.method_id, image_bytes("PNG"))
            cur.execute("SELECT COUNT(*) AS c FROM payment_assets WHERE payment_method_id=%s", (self.method_id,))
            self.assertEqual(cur.fetchone()["c"], 0)
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
