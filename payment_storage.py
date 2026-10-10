"""Private object storage abstraction for payment-method images.

Credentials are read only from environment variables. The default implementation
uses S3-compatible APIs; tests can inject InMemoryObjectStorage.
"""

import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, UnidentifiedImageError

ALLOWED_CONTENT_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
MAX_ASSET_BYTES = int(os.environ.get("PAYMENT_ASSET_MAX_BYTES", str(5 * 1024 * 1024)))


class ObjectStorage(Protocol):
    def upload(self, key: str, content: bytes, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def presign(self, key: str, expires_seconds: int = 300) -> str: ...


@dataclass(frozen=True)
class ValidatedImage:
    content: bytes
    content_type: str
    size: int
    sha256: str


def validate_image(content: bytes) -> ValidatedImage:
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("image_empty")
    content = bytes(content)
    if len(content) > MAX_ASSET_BYTES:
        raise ValueError("image_too_large")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            format_name = str(image.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("image_invalid")
    content_type = ALLOWED_CONTENT_TYPES.get(format_name)
    if not content_type:
        raise ValueError("image_type_not_allowed")
    return ValidatedImage(content, content_type, len(content), hashlib.sha256(content).hexdigest())


class S3ObjectStorage:
    def __init__(self, bucket=None, endpoint_url=None, region=None):
        import boto3
        self.bucket = bucket or os.environ.get("PAYMENT_ASSET_BUCKET", "").strip()
        self.endpoint_url = endpoint_url or os.environ.get("PAYMENT_ASSET_S3_ENDPOINT", "").strip() or None
        self.region = region or os.environ.get("PAYMENT_ASSET_S3_REGION", "").strip() or None
        if not self.bucket:
            raise RuntimeError("PAYMENT_ASSET_BUCKET missing")
        kwargs = {"service_name": "s3", "region_name": self.region}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        self.client = boto3.client(**kwargs)

    def upload(self, key, content, content_type):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type, ACL="private")

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def presign(self, key, expires_seconds=300):
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_seconds)


class InMemoryObjectStorage:
    def __init__(self):
        self.objects = {}

    def upload(self, key, content, content_type):
        self.objects[key] = (bytes(content), content_type)

    def delete(self, key):
        self.objects.pop(key, None)

    def presign(self, key, expires_seconds=300):
        if key not in self.objects:
            raise KeyError(key)
        return f"memory://private/{key}?expires={int(expires_seconds)}"


def upload_payment_asset(cur, storage, method_id, content):
    validated = validate_image(content)
    key = new_asset_key(method_id, validated)
    storage.upload(key, validated.content, validated.content_type)
    try:
        cur.execute(
            """INSERT INTO payment_assets(payment_method_id,storage_key,content_type,file_size,sha256,retention_status)
               VALUES(%s,%s,%s,%s,%s,'retained') RETURNING id""",
            (method_id, key, validated.content_type, validated.size, validated.sha256),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("payment_asset_insert_failed")
        return int(row["id"]), key, validated
    except Exception:
        try:
            storage.delete(key)
        finally:
            raise


def asset_has_protected_snapshot(cur, asset_id):
    cur.execute(
        """SELECT 1
           FROM cash_request_method_snapshots s
           JOIN cash_requests r ON r.id=s.cash_request_id
           WHERE s.asset_id=%s
             AND r.status IN ('pending','approved','funds_reserved','payout_in_progress','unknown_external_result','disputed')
           LIMIT 1""",
        (asset_id,),
    )
    return bool(cur.fetchone())


def retire_asset(cur, storage, asset_id):
    cur.execute("SELECT id,storage_key,retention_status FROM payment_assets WHERE id=%s FOR UPDATE", (asset_id,))
    asset = cur.fetchone()
    if not asset:
        raise ValueError("payment_asset_not_found")
    protected = asset_has_protected_snapshot(cur, asset_id)
    status = "retained" if protected else "eligible"
    cur.execute("UPDATE payment_assets SET is_active=FALSE,retention_status=%s,deleted_at=NULL WHERE id=%s RETURNING id", (status, asset_id))
    return {"asset_id": int(asset_id), "status": status, "storage_key": asset["storage_key"]}


def mark_asset_deleted(cur, storage, asset_id):
    cur.execute("SELECT id,storage_key FROM payment_assets WHERE id=%s FOR UPDATE", (asset_id,))
    asset = cur.fetchone()
    if not asset:
        raise ValueError("payment_asset_not_found")
    if asset_has_protected_snapshot(cur, asset_id):
        cur.execute("UPDATE payment_assets SET is_active=FALSE,retention_status='retained',deleted_at=NULL WHERE id=%s", (asset_id,))
        return {"asset_id": int(asset_id), "status": "retained", "storage_key": asset["storage_key"]}
    cur.execute("UPDATE payment_assets SET is_active=FALSE,retention_status='deleted',deleted_at=NOW() WHERE id=%s", (asset_id,))
    # Physical deletion is intentionally deferred to a retention worker. This keeps
    # rollback safe and avoids removing bytes needed by an old snapshot.
    return {"asset_id": int(asset_id), "status": "deleted", "storage_key": asset["storage_key"]}


def storage_from_environment():
    return S3ObjectStorage()


def new_asset_key(method_id, validated):
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[validated.content_type]
    return f"payment-methods/{int(method_id)}/{uuid.uuid4().hex}.{extension}"
