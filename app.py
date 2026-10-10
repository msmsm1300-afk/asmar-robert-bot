import os
import re
import html
import json
import base64
import hashlib
import hmac
import secrets
import time
import threading
import uuid
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import requests
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import ThreadedConnectionPool
from requests.adapters import HTTPAdapter
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, request, jsonify
from ichancy_provider import IchancyAgentClient
from payment_schema import install_payment_schema
from payment_storage import (
    MAX_ASSET_BYTES,
    mark_asset_deleted,
    retire_asset,
    storage_from_environment,
    upload_payment_asset,
    validate_image,
)
from payment_calculations import calculate_deposit_quote, calculate_withdraw_quote, validate_dynamic_fields, ichancy_deposit_units, ichancy_withdraw_bot_units, normalize_operation_reference
from payment_schema import (
    install_payment_schema,
    create_method_snapshot,
    reserve_withdrawal,
    approve_withdrawal,
    start_withdrawal_payout,
    record_external_outcome,
    settle_withdrawal,
    release_withdrawal,
    mark_withdrawal_unknown,
    record_financial_audit,
    record_external_verification,
    settle_topup_after_verification,
)

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or 0)
def _postgres_dsn(raw):
    """Render PostgreSQL requires TLS; make that explicit for every deployment."""
    value = (raw or "").strip()
    if value.startswith(("postgres://", "postgresql://")) and "sslmode=" not in value:
        return value + ("&" if "?" in value else "?") + "sslmode=require"
    return value


DATABASE_URL = _postgres_dsn(os.environ.get("DATABASE_URL", ""))
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://asmar-robert-bot.onrender.com"
).rstrip("/")
BRIDGE_SHARED_SECRET = os.environ.get("BRIDGE_SHARED_SECRET", "").strip()
ADMIN_BRIDGE_SHARED_SECRET = os.environ.get("ADMIN_BRIDGE_SHARED_SECRET", "").strip()
FINANCIAL_ADMIN_SERVICE_ID = os.environ.get("FINANCIAL_ADMIN_SERVICE_ID", "admin_service").strip() or "admin_service"
PLAYER_EMAIL_DOMAIN = os.environ.get("ICHANCY_PLAYER_EMAIL_DOMAIN", "asmarrobert.example").strip().lower()
REGISTRATION_MODE = os.environ.get("ICHANCY_REGISTRATION_MODE", "auto").strip().lower()
if REGISTRATION_MODE not in {"auto", "manual"}:
    REGISTRATION_MODE = "auto"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest() if BOT_TOKEN else ""

TG_SESSION = requests.Session()
TG_ADAPTER = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
TG_SESSION.mount("https://", TG_ADAPTER)

_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()
_DB_INITIALIZED = False
_BONUS_CACHE = {"expires": 0.0, "value": {}}
_SUPPORT_CACHE = {"expires": 0.0, "value": []}
_ICHANCY_CLIENT = None
_PAYMENT_STORAGE = None

flows = {}
panel_message_ids = {}
panel_message_kinds = {}
password_visible = {}

# UI-only fallback for development without PostgreSQL.
users_mem = {}
transactions_mem = []
bonuses_mem = {}

DEFAULT_BONUSES = {
    "sham": 0,
    "syriatel": 0,
    "usdt": 0,
    "wish": 0,
}

# Cash minimums: 200 SYP cash becomes 20,000 bot units (1:100).
MIN_TOPUP_SYP = 200
MIN_TOPUP_USD = Decimal("2")
MIN_BOT_WITHDRAW = 20_000
MIN_ICHANCY_DEPOSIT = 100
MIN_ICHANCY_WITHDRAW = 10_000
ICHANCY_UNITS_PER_BOT_UNIT = 100

# Cash-to-bot conversion: 1 SYP sent = 100 bot balance units.
SYP_TO_BOT_MULTIPLIER = 100

# Sham Cash SYP payment details.
SHAM_SYP_ACCOUNT_ID = "52b1612cc4685d57d0adb96d4d1be37a"
SHAM_SYP_ACCOUNT_NAME = "هشام محمد فتوح"
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
SHAM_SYP_QR_PATH = os.path.join(ASSET_DIR, "sham_cash_syp.jpg")


class ShamCashVerificationProvider:
    """Provider contract; no real Sham Cash integration exists locally."""

    name = "unconfigured"

    def verify(self, *_args, **_kwargs):
        raise RuntimeError("sham_cash_provider_not_configured")


class LocalTestShamCashVerificationProvider(ShamCashVerificationProvider):
    """Test-only adapter; it cannot be enabled by production configuration."""

    name = "local_test_mock"

    def verify(self, external_reference, amount_original, currency, beneficiary):
        if os.environ.get("LOCAL_TEST_MODE") != "1":
            raise RuntimeError("test_mock_disabled")
        return {
            "provider_name": self.name,
            "verification_status": "verified",
            "external_reference": str(external_reference),
            "amount_original": str(amount_original),
            "currency": str(currency),
            "beneficiary": str(beneficiary),
            "provider_trace_id": "local-test-only",
        }


def sham_cash_verification_provider():
    """Return only the local mock under an explicit local-test flag."""
    if os.environ.get("LOCAL_TEST_MODE") == "1":
        return LocalTestShamCashVerificationProvider()
    return ShamCashVerificationProvider()


# -----------------------------------------------------------------------------
# Security / DB helpers
# -----------------------------------------------------------------------------
def _fernet():
    raw = os.environ.get("APP_ENCRYPTION_KEY", "").strip()
    if raw:
        try:
            return Fernet(raw.encode())
        except Exception:
            pass
    digest = hashlib.sha256(BOT_TOKEN.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value):
    if value is None:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value):
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def db_enabled():
    return bool(DATABASE_URL)


def _get_db_pool():
    global _DB_POOL
    if _DB_POOL is not None:
        return _DB_POOL
    with _DB_POOL_LOCK:
        if _DB_POOL is None:
            _DB_POOL = ThreadedConnectionPool(
                1,
                8,
                DATABASE_URL,
                cursor_factory=RealDictCursor,
                connect_timeout=4,
                application_name="al-asmar-customer-bot",
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )
    return _DB_POOL


@contextmanager
def db_conn():
    pool = _get_db_pool()
    conn = pool.getconn()
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        try:
            if conn.closed:
                broken = True
        except Exception:
            broken = True
        pool.putconn(conn, close=broken)


def ensure_db():
    global _DB_INITIALIZED
    if not db_enabled() or _DB_INITIALIZED:
        return

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    telegram_username TEXT,
                    first_name TEXT,
                    balance BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
                    ichancy_username TEXT UNIQUE,
                    ichancy_password_enc TEXT,
                    ichancy_player_id TEXT UNIQUE,
                    ichancy_currency TEXT,
                    ichancy_creation_status TEXT,
                    ichancy_creation_job_id TEXT,
                    referred_by BIGINT,
                    referral_earnings BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ichancy_player_id TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ichancy_currency TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ichancy_creation_status TEXT")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ichancy_creation_job_id TEXT")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ichancy_player_id
                ON users (ichancy_player_id)
                WHERE ichancy_player_id IS NOT NULL
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    tx_type TEXT NOT NULL,
                    amount BIGINT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'completed',
                    method TEXT,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tx_code TEXT")
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS balance_before BIGINT")
            cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS balance_after BIGINT")
            cur.execute("""
                UPDATE transactions
                SET tx_code = 'TX-' || TO_CHAR(created_at, 'YYMMDD') || '-' ||
                    UPPER(SUBSTR(MD5(id::text || random()::text), 1, 8))
                WHERE tx_code IS NULL
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_tx_code
                ON transactions (tx_code)
                WHERE tx_code IS NOT NULL
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ichancy_username_lower
                ON users (LOWER(ichancy_username))
                WHERE ichancy_username IS NOT NULL
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_transactions_user_created
                ON transactions (telegram_id, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bonuses (
                    method TEXT PRIMARY KEY,
                    percent INTEGER NOT NULL DEFAULT 0 CHECK (percent >= 0 AND percent <= 1000),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS support_reps (
                    username TEXT PRIMARY KEY,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gift_codes (
                    code TEXT PRIMARY KEY,
                    amount BIGINT NOT NULL CHECK (amount > 0),
                    used_by BIGINT,
                    used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Shared with the admin bot. A Sham Cash request created here appears
            # immediately in the admin bot under top-up requests.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cash_requests (
                    id BIGSERIAL PRIMARY KEY,
                    request_code TEXT UNIQUE NOT NULL,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    request_type TEXT NOT NULL CHECK (request_type IN ('topup','withdraw')),
                    amount_bot BIGINT NOT NULL DEFAULT 0,
                    amount_cash BIGINT NOT NULL DEFAULT 0,
                    method TEXT,
                    destination TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cash_requests_type_status
                ON cash_requests(request_type,status,created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversation_flows (
                    telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                    flow JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bridge_devices (
                    device_id TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'offline',
                    ichancy_connected BOOLEAN NOT NULL DEFAULT FALSE,
                    last_heartbeat TIMESTAMPTZ,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bridge_jobs (
                    job_id UUID PRIMARY KEY,
                    request_id TEXT UNIQUE NOT NULL,
                    job_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    claimed_by TEXT,
                    result JSONB,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE bridge_jobs ADD COLUMN IF NOT EXISTS execution_attempt_id UUID")
            cur.execute("ALTER TABLE bridge_jobs ADD COLUMN IF NOT EXISTS external_attempt_started_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE bridge_jobs ADD COLUMN IF NOT EXISTS external_attempt_count INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE bridge_jobs ADD COLUMN IF NOT EXISTS reconciliation_required BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ichancy_registration_requests (
                    id BIGSERIAL PRIMARY KEY,
                    request_code TEXT UNIQUE NOT NULL,
                    telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                    username TEXT NOT NULL,
                    email TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    player_id TEXT,
                    admin_note TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_ichancy_registration_requests_status
                ON ichancy_registration_requests(status, created_at DESC)
            """)
            cur.execute("""
                INSERT INTO ichancy_registration_requests(request_code, telegram_id, username, email, status)
                SELECT 'REG-MIG-' || u.telegram_id::text, u.telegram_id, u.ichancy_username,
                       u.ichancy_username || '.' || u.telegram_id::text || '@' || %s, 'pending'
                FROM users u
                WHERE u.ichancy_player_id IS NULL
                  AND u.ichancy_username IS NOT NULL
                  AND u.ichancy_creation_status IN ('pending','running')
                ON CONFLICT (request_code) DO NOTHING
            """, (PLAYER_EMAIL_DOMAIN,))
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_bridge_jobs_pending
                ON bridge_jobs(status, created_at)
            """)
            if REGISTRATION_MODE == "auto":
                # Legacy deployments cancelled registration Jobs while forcing
                # manual mode. Mark only those known legacy requests as failed;
                # never recreate or requeue them automatically.
                cur.execute("""
                    UPDATE ichancy_registration_requests r
                    SET status='cancelled',
                        admin_note='legacy_manual_registration_mode',
                        updated_at=NOW()
                    WHERE r.status IN ('pending','queued','running')
                      AND EXISTS (
                          SELECT 1
                          FROM bridge_jobs j
                          WHERE j.job_type='register_player'
                            AND j.status='cancelled'
                            AND j.error='manual_registration_mode'
                            AND (j.payload->>'telegram_id')::bigint=r.telegram_id
                            AND LOWER(j.payload->>'username')=LOWER(r.username)
                      )
                """)
                cur.execute("""
                    UPDATE users u
                    SET ichancy_creation_status='failed',
                        ichancy_creation_job_id=NULL,
                        updated_at=NOW()
                    WHERE u.ichancy_player_id IS NULL
                      AND u.ichancy_creation_status='admin_pending'
                      AND EXISTS (
                          SELECT 1
                          FROM bridge_jobs j
                          WHERE j.job_type='register_player'
                            AND j.status='cancelled'
                            AND j.error='manual_registration_mode'
                            AND (j.payload->>'telegram_id')::bigint=u.telegram_id
                      )
                """)
            for method, percent in DEFAULT_BONUSES.items():
                cur.execute(
                    "INSERT INTO bonuses(method, percent) VALUES(%s, %s) ON CONFLICT(method) DO NOTHING",
                    (method, percent),
                )
            # Additive payment configuration and reservation schema. Existing
            # iChancy/Bridge tables and financial paths remain unchanged.
            install_payment_schema(cur)
    _DB_INITIALIZED = True


def load_persistent_flow(chat_id):
    if not db_enabled():
        return flows.get(chat_id)
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT flow FROM conversation_flows WHERE telegram_id=%s", (int(chat_id),))
            row = cur.fetchone()
            return (row.get("flow") if row else None) or None


def save_persistent_flow(chat_id, flow):
    if not db_enabled():
        return
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO conversation_flows(telegram_id, flow, updated_at)
                   VALUES(%s, %s::jsonb, NOW())
                   ON CONFLICT(telegram_id) DO UPDATE SET flow=EXCLUDED.flow, updated_at=NOW()""",
                (int(chat_id), json.dumps(flow, ensure_ascii=False)),
            )


def delete_persistent_flow(chat_id):
    if not db_enabled():
        return
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversation_flows WHERE telegram_id=%s", (int(chat_id),))


def get_bonuses():
    values = dict(DEFAULT_BONUSES)
    if not db_enabled():
        values.update({k: int(v) for k, v in bonuses_mem.items() if k in values})
        return values

    now = time.monotonic()
    cached = _BONUS_CACHE.get("value") or {}
    if cached and now < float(_BONUS_CACHE.get("expires", 0)):
        values.update(cached)
        return values

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT method, percent FROM bonuses")
            for row in cur.fetchall():
                method = row.get("method")
                if method in values:
                    values[method] = int(row.get("percent") or 0)
    _BONUS_CACHE["value"] = dict(values)
    _BONUS_CACHE["expires"] = now + 3.0
    return values


def upsert_user(chat_id, telegram_username=None, first_name=None, referred_by=None):
    if not db_enabled():
        if chat_id not in users_mem:
            users_mem[chat_id] = {
                "balance": 0,
                "ichancy_username": None,
                "ichancy_password": None,
                "ichancy_player_id": None,
                "ichancy_currency": None,
                "ichancy_creation_status": None,
                "ichancy_creation_job_id": None,
                "referred_by": referred_by,
                "referral_earnings": 0,
            }
        return

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id FROM users WHERE telegram_id=%s", (chat_id,))
            exists = cur.fetchone() is not None
            safe_ref = None
            if not exists and referred_by and int(referred_by) != int(chat_id):
                cur.execute("SELECT telegram_id FROM users WHERE telegram_id=%s", (int(referred_by),))
                if cur.fetchone():
                    safe_ref = int(referred_by)
            cur.execute("""
                INSERT INTO users(telegram_id, telegram_username, first_name, referred_by)
                VALUES(%s, %s, %s, %s)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    telegram_username=COALESCE(EXCLUDED.telegram_username, users.telegram_username),
                    first_name=COALESCE(EXCLUDED.first_name, users.first_name),
                    updated_at=NOW()
            """, (chat_id, telegram_username, first_name, safe_ref))


def get_user(chat_id):
    if not db_enabled():
        if chat_id not in users_mem:
            upsert_user(chat_id)
        u = dict(users_mem[chat_id])
        u["password_visible"] = password_visible.get(chat_id, False)
        return u

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id=%s", (chat_id,))
            row = cur.fetchone()
    if not row:
        upsert_user(chat_id)
        return get_user(chat_id)
    return {
        "balance": int(row["balance"] or 0),
        "ichancy_username": row["ichancy_username"],
        "ichancy_password": decrypt_secret(row["ichancy_password_enc"]),
        "ichancy_player_id": row.get("ichancy_player_id"),
        "ichancy_currency": row.get("ichancy_currency"),
        "ichancy_creation_status": row.get("ichancy_creation_status"),
        "ichancy_creation_job_id": row.get("ichancy_creation_job_id"),
        "password_visible": password_visible.get(chat_id, False),
        "referred_by": row["referred_by"],
        "referral_earnings": int(row["referral_earnings"] or 0),
    }


def save_ichancy_credentials(chat_id, username, password):
    if not db_enabled():
        get_user(chat_id)
        users_mem[chat_id]["ichancy_username"] = username
        users_mem[chat_id]["ichancy_password"] = password
        return True, None

    ensure_db()
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET ichancy_username=%s, ichancy_password_enc=%s, updated_at=NOW()
                    WHERE telegram_id=%s
                """, (username, encrypt_secret(password), chat_id))
        return True, None
    except psycopg2.errors.UniqueViolation:
        return False, "username_exists"


def save_ichancy_player_mapping(chat_id, player_id, username=None, currency=None):
    """Persist the real iChancy Player mapping; never stores an access token."""
    player_id = str(player_id).strip()
    if not player_id:
        return False, "invalid_player_id"
    if not db_enabled():
        get_user(chat_id)
        users_mem[chat_id]["ichancy_player_id"] = player_id
        users_mem[chat_id]["ichancy_currency"] = currency
        if username:
            users_mem[chat_id]["ichancy_username"] = username
        return True, None

    ensure_db()
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users
                    SET ichancy_player_id=%s,
                        ichancy_username=COALESCE(%s, ichancy_username),
                        ichancy_currency=COALESCE(%s, ichancy_currency),
                        updated_at=NOW()
                    WHERE telegram_id=%s
                """, (player_id, username, currency, chat_id))
        return True, None
    except psycopg2.errors.UniqueViolation:
        return False, "player_exists"


def create_manual_registration_request(chat_id, username, password):
    """Save credentials and create an admin-only registration request."""
    if not db_enabled():
        return {"ok": False, "reason": "postgres_required"}
    ensure_db()
    user = get_user(chat_id)
    if user.get("ichancy_player_id"):
        return {"ok": False, "reason": "already_mapped"}
    if user.get("ichancy_creation_status") in {"pending", "running", "admin_pending"}:
        return {"ok": False, "reason": "already_pending"}
    ok, reason = save_ichancy_credentials(chat_id, username, password)
    if not ok:
        return {"ok": False, "reason": reason or "username_exists"}
    request_code = f"REG-{datetime.now(timezone.utc).strftime('%y%m%d')}-{secrets.token_hex(4).upper()}"
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ichancy_registration_requests
                    (request_code, telegram_id, username, email, status)
                VALUES(%s,%s,%s,%s,'pending')
                RETURNING id, request_code
            """, (request_code, int(chat_id), username, generated_player_email(username, chat_id)))
            row = cur.fetchone()
            cur.execute("""
                UPDATE users
                SET ichancy_creation_status='admin_pending', ichancy_creation_job_id=NULL, updated_at=NOW()
                WHERE telegram_id=%s
            """, (int(chat_id),))
    return {"ok": True, "request_code": row["request_code"]}


def registration_mode():
    """Return the explicitly configured registration mode.

    Automatic Bridge registration is the safe default. The old implementation
    silently forced manual mode from ensure_db(), which cancelled live Jobs.
    """
    return REGISTRATION_MODE


def registration_status_message(status):
    """Customer-safe status text for the registration state machine."""
    if status == "admin_pending":
        return (
            "🕒 <b>تم استلام طلب إنشاء الحساب</b>\n\n"
            "الطلب بانتظار إجراء إداري، وستصلك رسالة عند اكتماله."
        )
    if status in {"pending", "running"}:
        return (
            "⏳ <b>جاري إنشاء حسابك في iChancy…</b>\n\n"
            "يرجى الانتظار قليلًا، وستصلك النتيجة تلقائيًا."
        )
    return ""


def ichancy_client():
    """Return one process-wide Agent API client; never sign in per request."""
    global _ICHANCY_CLIENT
    if _ICHANCY_CLIENT is None:
        _ICHANCY_CLIENT = IchancyAgentClient()
    return _ICHANCY_CLIENT


def fetch_ichancy_balance(chat_id):
    """Read the real iChancy balance for a mapped Player, without mutations."""
    user = get_user(chat_id)
    player_id = user.get("ichancy_player_id")
    if not player_id:
        return {"ok": False, "reason": "player_not_mapped"}
    try:
        payload = ichancy_client().get_player_balance(player_id)
        result = payload.get("result") or []
        main = next((item for item in result if item.get("main")), result[0] if result else None)
        if not main:
            return {"ok": False, "reason": "balance_not_found"}
        return {
            "ok": True,
            "balance": main.get("balance"),
            "currency": main.get("currencyCode") or user.get("ichancy_currency"),
        }
    except Exception as exc:
        # Do not expose credentials, tokens, or upstream response bodies to Telegram.
        return {"ok": False, "reason": type(exc).__name__}


def username_taken(username, except_chat_id=None):
    if not db_enabled():
        for cid, u in users_mem.items():
            if cid != except_chat_id and (u.get("ichancy_username") or "").lower() == username.lower():
                return True
        return False

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            if except_chat_id is not None:
                cur.execute("""
                    SELECT 1 FROM users
                    WHERE LOWER(ichancy_username)=LOWER(%s) AND telegram_id<>%s
                    LIMIT 1
                """, (username, except_chat_id))
            else:
                cur.execute("""
                    SELECT 1 FROM users
                    WHERE LOWER(ichancy_username)=LOWER(%s)
                    LIMIT 1
                """, (username,))
            return cur.fetchone() is not None


def make_tx_code(prefix="TX"):
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%y%m%d')}-{secrets.token_hex(4).upper()}"


def add_transaction(chat_id, tx_type, amount=0, status="completed", method=None, details=None,
                    balance_before=None, balance_after=None, conn=None, tx_code=None):
    details = details or {}
    tx_code = tx_code or make_tx_code()

    if not db_enabled():
        transactions_mem.append({
            "telegram_id": chat_id,
            "tx_code": tx_code,
            "tx_type": tx_type,
            "amount": int(amount),
            "status": status,
            "method": method,
            "details": details,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "created_at": datetime.now(timezone.utc),
        })
        return tx_code

    ensure_db()

    def _insert(connection):
        code = tx_code
        for _ in range(3):
            try:
                with connection.cursor() as cur:
                    cur.execute("""
                        INSERT INTO transactions(
                            telegram_id, tx_code, tx_type, amount, status, method, details,
                            balance_before, balance_after
                        )
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        chat_id, code, tx_type, int(amount), status, method, Json(details),
                        balance_before, balance_after,
                    ))
                return code
            except psycopg2.errors.UniqueViolation:
                connection.rollback()
                code = make_tx_code()
        raise RuntimeError("could not generate unique transaction code")

    if conn is not None:
        return _insert(conn)
    with db_conn() as connection:
        return _insert(connection)


def list_transactions(chat_id, limit=10, offset=0, tx_types=None):
    tx_types = list(tx_types or [])
    if not db_enabled():
        items = [x for x in transactions_mem if x["telegram_id"] == chat_id]
        if tx_types:
            items = [x for x in items if x.get("tx_type") in tx_types]
        return items[::-1][int(offset):int(offset) + int(limit)]

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            if tx_types:
                cur.execute("""
                    SELECT tx_code, tx_type, amount, status, method, details,
                           balance_before, balance_after, created_at
                    FROM transactions
                    WHERE telegram_id=%s AND tx_type = ANY(%s)
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """, (chat_id, tx_types, int(limit), int(offset)))
            else:
                cur.execute("""
                    SELECT tx_code, tx_type, amount, status, method, details,
                           balance_before, balance_after, created_at
                    FROM transactions
                    WHERE telegram_id=%s
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """, (chat_id, int(limit), int(offset)))
            return list(cur.fetchall())


def redeem_gift_code(chat_id, raw_code):
    code = (raw_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return {"ok": False, "reason": "invalid"}
    if not db_enabled():
        return {"ok": False, "reason": "storage_unavailable"}

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, amount, used_by FROM gift_codes WHERE code=%s FOR UPDATE",
                (code,),
            )
            gift = cur.fetchone()
            if not gift:
                return {"ok": False, "reason": "invalid"}
            if gift["used_by"] is not None:
                return {"ok": False, "reason": "used"}

            cur.execute("SELECT balance FROM users WHERE telegram_id=%s FOR UPDATE", (chat_id,))
            user = cur.fetchone()
            if not user:
                raise RuntimeError("user missing")
            amount = int(gift["amount"] or 0)
            before = int(user["balance"] or 0)
            after = before + amount

            cur.execute("UPDATE users SET balance=%s, updated_at=NOW() WHERE telegram_id=%s", (after, chat_id))
            cur.execute(
                "UPDATE gift_codes SET used_by=%s, used_at=NOW() WHERE code=%s AND used_by IS NULL",
                (chat_id, code),
            )
            if cur.rowcount != 1:
                return {"ok": False, "reason": "used"}

        tx_code = add_transaction(
            chat_id, "gift", amount, "completed", "gift-code",
            {"gift_code": code}, before, after, conn=conn,
        )

    return {
        "ok": True,
        "code": code,
        "amount": amount,
        "before": before,
        "after": after,
        "tx_code": tx_code,
    }


def referral_stats(chat_id):
    if not db_enabled():
        count = sum(1 for u in users_mem.values() if u.get("referred_by") == chat_id)
        return count, int(get_user(chat_id).get("referral_earnings", 0))

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE referred_by=%s", (chat_id,))
            count = int(cur.fetchone()["c"])
            cur.execute("SELECT referral_earnings FROM users WHERE telegram_id=%s", (chat_id,))
            row = cur.fetchone()
            earnings = int(row["referral_earnings"] or 0) if row else 0
    return count, earnings


def db_support_usernames():
    now = time.monotonic()
    if now < float(_SUPPORT_CACHE.get("expires", 0)):
        return list(_SUPPORT_CACHE.get("value") or [])

    names = []
    if db_enabled():
        ensure_db()
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT username FROM support_reps WHERE is_active=TRUE ORDER BY sort_order, username")
                names = [r["username"] for r in cur.fetchall()]
    _SUPPORT_CACHE["value"] = list(names)
    _SUPPORT_CACHE["expires"] = now + 5.0
    return names


def create_sham_syp_topup_request(chat_id, cash_amount, operation_ref):
    """Create one pending Sham Cash SYP request for the admin bot.

    No balance is credited here. This only records the customer's transfer claim
    and expected bot credit. Admin/API verification must complete it later.
    """
    if not db_enabled():
        return {"ok": False, "reason": "storage_unavailable"}

    ensure_db()
    operation_ref = normalize_operation_reference(operation_ref)
    bonus_percent = int(get_bonuses().get("sham", 0))
    base_bot = int(cash_amount) * SYP_TO_BOT_MULTIPLIER
    bonus_amount = (base_bot * bonus_percent) // 100
    total_bot = base_bot + bonus_amount
    request_code = make_tx_code("DEP")
    method = "sham_syp"
    details = {
        "operation_ref": operation_ref,
        "currency": "SYP",
        "base_bot": base_bot,
        "bonus_percent": bonus_percent,
        "bonus_amount": bonus_amount,
        "account_id": SHAM_SYP_ACCOUNT_ID,
        "account_name": SHAM_SYP_ACCOUNT_NAME,
    }

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO cash_requests(
                        request_code, telegram_id, request_type, amount_bot,
                        amount_cash, method, destination, status, details
                    )
                    VALUES(%s,%s,'topup',%s,%s,%s,%s,'pending',%s)
                    RETURNING id
                """, (
                    request_code,
                    chat_id,
                    total_bot,
                    int(cash_amount),
                    method,
                    SHAM_SYP_ACCOUNT_ID,
                    Json(details),
                ))
                cash_request_id = int(cur.fetchone()["id"])

            tx_code = add_transaction(
                chat_id,
                "bot_topup",
                total_bot,
                "pending",
                method,
                {**details, "request_code": request_code, "cash_amount": int(cash_amount),
                 "cash_request_id": cash_request_id},
                None,
                None,
                conn=conn,
            )
            with conn.cursor() as link_cur:
                link_cur.execute("UPDATE transactions SET cash_request_id=%s WHERE tx_code=%s", (cash_request_id, tx_code))
    except psycopg2.errors.UniqueViolation:
        return {"ok": False, "reason": "duplicate_ref"}

    return {
        "ok": True,
        "request_code": request_code,
        "tx_code": tx_code,
        "cash_amount": int(cash_amount),
        "base_bot": base_bot,
        "bonus_percent": bonus_percent,
        "bonus_amount": bonus_amount,
        "total_bot": total_bot,
        "operation_ref": operation_ref,
    }


def verify_and_settle_sham_syp_for_local_test(cash_request_id, external_reference,
                                              amount_original, currency,
                                              beneficiary, actor_id="local-test"):
    """Test-only provider path; production stays disabled without a real provider."""
    provider = sham_cash_verification_provider()
    evidence = provider.verify(external_reference, amount_original, currency, beneficiary)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM cash_requests WHERE id=%s FOR UPDATE", (int(cash_request_id),))
            request_row = cur.fetchone()
            if not request_row:
                raise ValueError("topup_request_not_found")
            if request_row["status"] not in {"pending", "approved", "externally_verified"}:
                raise ValueError("topup_not_verifiable")
            record_external_verification(
                cur, int(cash_request_id), evidence["provider_name"],
                evidence["verification_status"], evidence["external_reference"],
                evidence["amount_original"], evidence["currency"],
                evidence["beneficiary"], evidence["provider_trace_id"],
                {"test_only": True},
            )
            cur.execute(
                """UPDATE cash_requests
                   SET status='externally_verified',external_outcome='external_confirmed_success',updated_at=NOW()
                 WHERE id=%s AND status IN ('pending','approved','externally_verified')""",
                (int(cash_request_id),),
            )
            if cur.rowcount != 1:
                raise RuntimeError("topup_verification_transition_failed")
            record_financial_audit(
                cur, actor_id, "test_provider", "external_verification",
                int(cash_request_id), "topup", request_row["status"],
                "externally_verified", "verified", "local_test_provider_only",
                None, evidence["external_reference"], {"test_only": True},
            )
            settled = settle_topup_after_verification(
                cur, int(cash_request_id), evidence["provider_name"],
                evidence["external_reference"], evidence["amount_original"],
                evidence["currency"], evidence["beneficiary"], actor_id,
                "test_provider",
            )
            return {"verified": True, "settled": bool(settled), "test_only": True}


# -----------------------------------------------------------------------------
# Telegram helpers
# -----------------------------------------------------------------------------
def _telegram_safe_error(method, status_code=None, payload=None):
    """Log only bounded, non-sensitive Telegram error metadata."""
    payload = payload if isinstance(payload, dict) else {}
    code = payload.get("error_code") or payload.get("code") or "telegram_error"
    code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(code))[:80]
    trace_id = payload.get("trace_id") or payload.get("request_id") or payload.get("error_id")
    trace_id = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(trace_id))[:80] if trace_id else "-"
    http = str(status_code) if status_code is not None else "-"
    print(f"telegram_error method={method} http={http} code={code} trace={trace_id}", flush=True)


def _safe_exception_metadata(operation, exc):
    """Emit only non-sensitive exception metadata and return a trace id."""
    trace_id = uuid.uuid4().hex[:12]
    exception_type = re.sub(r"[^A-Za-z0-9_.:-]", "_", type(exc).__name__)[:80]
    print(f"exception operation={operation} type={exception_type} trace={trace_id}", flush=True)
    return trace_id


def _safe_public_error(exc, fallback):
    """Keep only predefined-looking error codes, never arbitrary exception text."""
    value = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.:-]{0,79}", value):
        return value
    return fallback


def _customer_job_outcome_message(job_type, status):
    """Return customer-safe status text; never expose Bridge/provider error text."""
    if job_type == "deposit":
        if status == "unknown_external_result":
            return "⚠️ <b>نتيجة شحن حساب iChancy غير محسومة حاليًا</b>\n\nتم إيقاف إعادة التنفيذ تلقائيًا، ولم يُخصم المبلغ حتى تُحسم النتيجة بالمصالحة."
        return "⚠️ <b>لم يكتمل شحن حساب iChancy</b>\n\nلم يتم خصم المبلغ من رصيدك. يمكنك المحاولة لاحقًا بعد التأكد من جاهزية الجسر."
    if job_type == "withdraw":
        if status == "unknown_external_result":
            return "⚠️ <b>نتيجة سحب iChancy غير محسومة حاليًا</b>\n\nتم تعليق العملية وإحالتها إلى المصالحة، ولم تتم إضافة أي رصيد إلى البوت أو إعادة تنفيذ السحب تلقائيًا."
        return "⚠️ <b>لم يكتمل سحب iChancy</b>\n\nلم تتم إضافة أي رصيد إلى البوت. يمكنك المحاولة لاحقًا بعد التأكد من جاهزية الجسر."
    if job_type == "get_balance":
        return "⚠️ <b>تعذر تحديث الرصيد من iChancy</b>\n\nحاول مرة أخرى لاحقًا."
    return "⚠️ <b>لم تكتمل العملية</b>\n\nحاول مرة أخرى لاحقًا."


def tg(method, payload=None, timeout=4):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    try:
        r = TG_SESSION.post(
            f"{TG_API}/{method}",
            json=payload or {},
            timeout=(3.0, float(timeout)),
        )
        try:
            data = r.json()
        except Exception:
            data = {"ok": False, "description": f"Telegram HTTP {r.status_code}"}
        if not data.get("ok"):
            _telegram_safe_error(method, r.status_code, data)
        return data
    except Exception as exc:
        trace_id = _safe_exception_metadata(f"telegram.{method}", exc)
        return {"ok": False, "description": "telegram_request_failed", "trace_id": trace_id}


def download_telegram_photo(photo_sizes):
    if not photo_sizes:
        raise ValueError("proof_image_required")
    file_info = tg("getFile", {"file_id": photo_sizes[-1].get("file_id")}, timeout=10)
    path = ((file_info.get("result") or {}).get("file_path")) if file_info.get("ok") else None
    if not path:
        raise ValueError("proof_image_unavailable")
    response = TG_SESSION.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=(3.0, 20.0))
    response.raise_for_status()
    return response.content


def ensure_native_menu():
    tg("setMyCommands", {"commands": [{"command": "start", "description": "START"}]})
    tg("setChatMenuButton", {"menu_button": {"type": "commands"}})


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)


def delete_message(chat_id, message_id):
    return tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=2)


def answer_callback(callback_id, text=None, alert=False):
    payload = {"callback_query_id": callback_id, "show_alert": alert}
    if text:
        payload["text"] = text
    return tg("answerCallbackQuery", payload, timeout=2.5)


def send_photo_file(chat_id, path, caption, reply_markup=None):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN missing"}
    if not os.path.exists(path):
        return {"ok": False, "description": f"photo missing: {os.path.basename(path)}"}

    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    try:
        with open(path, "rb") as photo:
            r = TG_SESSION.post(
                f"{TG_API}/sendPhoto",
                data=data,
                files={"photo": (os.path.basename(path), photo, "image/jpeg")},
                timeout=(3.0, 12.0),
            )
        result = r.json()
        if not result.get("ok"):
            _telegram_safe_error("sendPhoto", r.status_code, result)
        return result
    except Exception as exc:
        trace_id = _safe_exception_metadata("telegram.sendPhoto", exc)
        return {"ok": False, "description": "telegram_request_failed", "trace_id": trace_id}


def inline(rows):
    return {"inline_keyboard": rows}


def cb(text, data):
    return {"text": text, "callback_data": data}


def nav_row(back_data="home", back_label="🔙 رجوع"):
    return [cb(back_label, back_data)]


def url_btn(text, url):
    return {"text": text, "url": url}


def copy_btn(text, value):
    return {"text": text, "copy_text": {"text": str(value)}}


def fmt_amount(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def set_panel(chat_id, text, reply_markup=None, force_new=False):
    mid = panel_message_ids.get(chat_id)
    kind = panel_message_kinds.get(chat_id, "text")

    if mid and not force_new and kind == "text":
        res = edit_message(chat_id, mid, text, reply_markup)
        if res.get("ok"):
            return res
        desc = str(res.get("description", "")).lower()
        if "message is not modified" in desc:
            return {"ok": True, "result": {"message_id": mid}}

    # If the active panel was a photo, remove it before switching back to text.
    if mid and kind == "photo" and not force_new:
        delete_message(chat_id, mid)

    res = send_message(chat_id, text, reply_markup)
    if res.get("ok") and res.get("result"):
        panel_message_ids[chat_id] = res["result"]["message_id"]
        panel_message_kinds[chat_id] = "text"
    return res


def set_photo_panel(chat_id, path, caption, reply_markup=None):
    mid = panel_message_ids.get(chat_id)
    if mid:
        delete_message(chat_id, mid)
    res = send_photo_file(chat_id, path, caption, reply_markup)
    if res.get("ok") and res.get("result"):
        panel_message_ids[chat_id] = res["result"]["message_id"]
        panel_message_kinds[chat_id] = "photo"
    return res


# -----------------------------------------------------------------------------
# Customer UI
# -----------------------------------------------------------------------------
def main_inline_keyboard():
    return inline([
        [cb("🎮 حساب iChancy 🎮", "account")],
        [cb("⬇️ شحن رصيد البوت", "topup"), cb("⬆️ سحب رصيد البوت", "withdraw_bot")],
        [cb("🎮 شحن حساب iChancy", "ichancy_deposit"), cb("💸 سحب من حساب iChancy", "ichancy_withdraw")],
        [cb("📋 سجل العمليات", "history"), cb("🎁 العروض والبونصات", "offers")],
        [cb("👥 نظام الإحالات", "referrals"), cb("🎟️ كود الهدية", "gift")],
        [cb("💬 الدعم والمساعدة", "support"), cb("📜 الشروط والخدمات", "terms")],
    ])


def greeting(chat_id):
    u = get_user(chat_id)
    return (
        "👑 <b>اهــــلا بالمــــــلك</b> 👑\n"
        f"🆔 معرفك على البوت: <code>{chat_id}</code>\n"
        f"💰 رصيدك: <b>{fmt_amount(u['balance'])}</b>\n"
        "👑 <b>نفتخر بانضمامك يامــلك</b> 👑"
    )


def show_home(chat_id, force_new=False):
    flows.pop(chat_id, None)
    delete_persistent_flow(chat_id)
    return set_panel(chat_id, greeting(chat_id), main_inline_keyboard(), force_new=force_new)


def show_account(chat_id, created=False):
    u = get_user(chat_id)
    flows.pop(chat_id, None)
    delete_persistent_flow(chat_id)
    if not u["ichancy_player_id"] and u.get("ichancy_creation_status") in {"pending", "running", "admin_pending"}:
        return set_panel(
            chat_id,
            registration_status_message(u.get("ichancy_creation_status")),
            inline([nav_row("home")]),
        )
    if not u["ichancy_player_id"] and not u["ichancy_username"]:
        return set_panel(
            chat_id,
            "🎮 <b>حساب iChancy</b> 🎮\n\nلا يوجد حساب iChancy مرتبط بحسابك حاليًا.",
            inline([[cb("➕ إنشاء حساب جديد", "ichancy_create")], nav_row("home")]),
        )

    if not u["ichancy_player_id"]:
        return set_panel(
            chat_id,
            "⚠️ <b>لا يوجد حساب iChancy مرتبط حاليًا.</b>\n\nلم يكتمل آخر طلب إنشاء، ولم يتم اعتماد بيانات الدخول. يمكنك المحاولة من جديد.",
            inline([[cb("➕ إنشاء حساب جديد", "ichancy_create")], nav_row("home")]),
        )

    pwd = u["ichancy_password"] or ""
    shown = html.escape(pwd) if u.get("password_visible") else "••••••••"
    username = html.escape(u["ichancy_username"] or "غير متاح")
    title = "✅ <b>تم إنشاء حسابك بنجاح</b>" if created else "🔐 <b>بيانات تسجيل الدخول للحساب</b>"
    balance_line = "\n💰 الرصيد الحقيقي: <i>اضغط «تحديث الرصيد» لجلبه من iChancy</i>\n"
    text = (
        f"{title}\n\n"
        f"👤 اسم المستخدم: <code>{username}</code>\n"
        f"🔑 كلمة المرور: <code>{shown}</code>\n"
        f"🆔 Player ID: <code>{html.escape(str(u.get('ichancy_player_id') or 'غير مربوط'))}</code>\n"
        f"{balance_line}"
    )
    toggle = "🙈 إخفاء كلمة المرور" if u.get("password_visible") else "👁 عرض كلمة المرور"
    return set_panel(chat_id, text, inline([
        [copy_btn("📋 نسخ اسم المستخدم", u["ichancy_username"])],
        [cb(toggle, "ichancy_toggle_password")],
        [copy_btn("📋 نسخ كلمة المرور", pwd)],
        [cb("🔄 تحديث الرصيد", "ichancy_balance")],
        [url_btn("🌐 الدخول إلى iChancy", "https://www.ichancy200.com")],
        nav_row("home"),
    ]))


def show_topup(chat_id):
    methods = dynamic_payment_methods("deposit")
    if methods:
        rows = [[cb(f"💳 {method['display_name']}", f"dyn_dep:{method['method_id']}")] for method in methods]
        rows.append(nav_row("home"))
        return set_panel(chat_id, "💳 <b>اختر طريقة الإيداع</b>\n\nتظهر هنا الطرق المفعلة فقط.", inline(rows))
    u = get_user(chat_id)
    b = get_bonuses()
    text = (
        "💳 <b>شحن رصيد البوت</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "🎁 <b>البونصات المتاحة حاليًا</b>\n"
        f"💸 Sham Cash: <b>+{b['sham']}%</b>\n"
        f"🔴 Syriatel Cash: <b>+{b['syriatel']}%</b>\n"
        f"🟢 USDT: <b>+{b['usdt']}%</b>\n"
        f"🟣 Wish Money: <b>+{b['wish']}%</b>\n\n"
        "اختر وسيلة الشحن المناسبة:"
    )
    return set_panel(chat_id, text, inline([
        [cb("💸 Sham Cash", "topup_sham"), cb("🔴 Syriatel Cash", "topup_syriatel")],
        [cb("🟢 USDT", "topup_usdt"), cb("🟣 Wish Money", "topup_wish")],
        nav_row("home"),
    ]))


def show_sham_cash_currency(chat_id):
    return set_panel(
        chat_id,
        "💸 <b>Sham Cash</b>\n\nاختر العملة التي تريد الإيداع بها:",
        inline([
            [cb("💸 Sham Cash ليرة", "topup_sham_syp")],
            [cb("💲 Sham Cash Dollar", "topup_sham_usd")],
            nav_row("topup"),
        ]),
    )


def show_sham_syp_payment(chat_id):
    b = get_bonuses()
    bonus = int(b.get("sham", 0))
    caption = (
        "💸 <b>Sham Cash ليرة</b>\n\n"
        f"👤 اسم الحساب: <b>{html.escape(SHAM_SYP_ACCOUNT_NAME)}</b>\n"
        f"🆔 معرف الحساب:\n<code>{SHAM_SYP_ACCOUNT_ID}</code>\n\n"
        f"🔻 الحد الأدنى للإيداع: <b>{fmt_amount(MIN_TOPUP_SYP)} ل.س</b>\n"
        f"🎁 البونص الحالي: <b>+{bonus}%</b>\n\n"
        "حوّل المبلغ إلى الحساب الظاهر بالصورة، وبعد إتمام التحويل اضغط <b>تم التحويل</b>."
    )
    markup = inline([
        [copy_btn("📋 نسخ معرف Sham Cash", SHAM_SYP_ACCOUNT_ID)],
        [cb("✅ تم التحويل", "sham_syp_done")],
        nav_row("topup_sham"),
    ])
    res = set_photo_panel(chat_id, SHAM_SYP_QR_PATH, caption, markup)
    if not res.get("ok"):
        # Safe fallback if the image file was forgotten during upload.
        fallback = (
            caption
            + "\n\n⚠️ <i>صورة QR غير موجودة على السيرفر، استخدم معرف الحساب أعلاه.</i>"
        )
        return set_panel(chat_id, fallback, markup)
    return res


def show_usdt_networks(chat_id):
    return set_panel(chat_id, (
        "🟢 <b>الشحن عبر USDT</b>\n\n"
        "💵 الحد الأدنى للإيداع: <b>$2</b>\n\n"
        "اختر شبكة التحويل:"
    ), inline([
        [cb("🔴 USDT TRC20", "usdt_trc20")],
        [cb("🟡 USDT BEP20", "usdt_bep20")],
        nav_row("topup"),
    ]))


def show_topup_amount_prompt(chat_id, method_name, currency, minimum, back_data="topup"):
    flows[chat_id] = {
        "step": "bot_topup_amount",
        "method": method_name,
        "currency": currency,
        "minimum": str(minimum),
        "back_data": back_data,
    }
    if currency == "SYP":
        minimum_text = f"{fmt_amount(minimum)} ل.س"
        prompt = "أدخل مبلغ الإيداع بالليرة السورية."
    else:
        minimum_text = f"${minimum}"
        prompt = "أدخل مبلغ الإيداع بالدولار."
    return set_panel(chat_id, (
        f"💳 <b>{html.escape(method_name)}</b>\n\n"
        f"🔻 الحد الأدنى للإيداع: <b>{minimum_text}</b>\n\n"
        f"{prompt}"
    ), inline([nav_row(back_data, "🔙 إلغاء")]))


def show_topup_ready(chat_id, method_name, currency, amount):
    amount_text = (
        f"{fmt_amount(int(amount))} ل.س"
        if currency == "SYP"
        else f"${format(amount, 'f').rstrip('0').rstrip('.')}"
    )
    return set_panel(chat_id, (
        "✅ <b>تم قبول المبلغ</b>\n\n"
        f"💳 الطريقة: <b>{html.escape(method_name)}</b>\n"
        f"💰 المبلغ: <b>{amount_text}</b>\n\n"
        "🚧 سيتم إضافة صورة وتعليمات التحويل لهذه الطريقة لاحقًا."
    ), inline([nav_row("topup")]))


def show_withdraw_bot(chat_id):
    methods = dynamic_payment_methods("withdraw")
    if methods:
        rows = [[cb(f"💸 {method['display_name']}", f"dyn_wd:{method['method_id']}")] for method in methods]
        rows.append(nav_row("home"))
        return set_panel(chat_id, "💸 <b>اختر طريقة السحب</b>\n\nتظهر هنا الطرق المفعلة للسحب فقط.", inline(rows))
    u = get_user(chat_id)
    return set_panel(chat_id, (
        "💸 <b>سحب رصيد البوت</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "اختر طريقة السحب المناسبة:"
    ), inline([
        [cb("💸 Sham Cash", "wd_sham"), cb("🔴 Syriatel Cash", "wd_syriatel")],
        [cb("🟢 USDT", "wd_usdt"), cb("🟣 Wish Money", "wd_wish")],
        nav_row("home"),
    ]))


def show_withdraw_method(chat_id, method_name):
    u = get_user(chat_id)
    flows[chat_id] = {"step": "withdraw_bot_amount", "method": method_name}
    return set_panel(chat_id, (
        f"💸 <b>السحب عبر {html.escape(method_name)}</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
        f"🔻 الحد الأدنى للسحب: <b>{fmt_amount(MIN_BOT_WITHDRAW)}</b>\n\n"
        "أدخل المبلغ المطلوب سحبه من رصيد البوت."
    ), inline([nav_row("withdraw_bot", "🔙 إلغاء")]))


def _dynamic_method(method_id, direction):
    return next((m for m in dynamic_payment_methods(direction) if int(m["method_id"]) == int(method_id)), None)


def _dynamic_prompt_next(chat_id):
    flow = flows.get(chat_id) or {}
    remaining = flow.get("remaining_fields") or []
    if not remaining:
        try:
            result = create_dynamic_cash_request(chat_id, flow["method"], flow["direction"], flow["values"], flow.get("attachment_content"))
            flows.pop(chat_id, None)
            quote = result["quote"]
            amount = quote.get("bot_amount", quote.get("payout_amount"))
            label = "سيضاف إلى رصيدك" if flow["direction"] == "deposit" else "المبلغ المتوقع استلامه"
            return set_panel(chat_id, f"✅ <b>تم استلام طلبك</b>\n\nرقم الطلب: <code>{result['request_code']}</code>\n{label}: <b>{fmt_amount(amount)}</b>\n\nسيتم التحقق من الطلب قبل أي تسوية مالية.", inline([nav_row("home")]))
        except ValueError as exc:
            flows.pop(chat_id, None)
            error_code = _safe_public_error(exc, "invalid_request")
            if error_code in {"amount_below_minimum", "amount_above_maximum"}:
                message = "المبلغ خارج الحدود المسموحة."
            elif error_code.startswith("operation_reference_"):
                message = "رقم العملية غير صالح أو مفقود."
            else:
                message = "البيانات المطلوبة غير مكتملة."
            return set_panel(chat_id, f"⚠️ {message}", inline([nav_row("home")]))
        except psycopg2.errors.UniqueViolation:
            flows.pop(chat_id, None)
            return set_panel(chat_id, "❌ رقم العملية مكرر أو مستخدم مسبقًا. يرجى التأكد من الرقم وإرسال رقم عملية لم يُستخدم من قبل.", inline([nav_row("home")]))
        except Exception:
            flows.pop(chat_id, None)
            return set_panel(chat_id, "⚠️ تعذر حفظ الطلب حاليًا. لم يتم إنشاء طلب ناقص.", inline([nav_row("home")]))
    key = remaining.pop(0)
    flow["current_field"] = key
    if key == "proof_image":
        prompt = "🧾 أرسل صورة إثبات التحويل."
    else:
        prompt = {"operation_reference": "🧾 اكتب رقم العملية.", "note": "📝 اكتب ملاحظة اختيارية.", "recipient_account": "🏦 اكتب حساب الاستلام.", "recipient_name": "👤 اكتب اسم المستلم.", "network": "🌐 اكتب الشبكة."}.get(key, "✏️ أرسل البيانات المطلوبة.")
    mode = (flow.get("fields") or {}).get(key)
    suffix = "\nيمكنك التخطي." if mode == "optional" else ""
    buttons = [[cb("⏭ تخطي", f"dyn_skip:{chat_id}")]] if mode == "optional" else []
    buttons.append(nav_row("home"))
    return set_panel(chat_id, prompt + suffix, inline(buttons))


def _start_dynamic_payment(chat_id, method_id, direction):
    method = _dynamic_method(method_id, direction)
    if not method:
        return set_panel(chat_id, "⚠️ طريقة الدفع غير متاحة حاليًا.", inline([nav_row("home")]))
    fields = method.get("fields") or {}
    ordered = [key for key in ("operation_reference", "proof_image", "note", "recipient_account", "recipient_name", "network") if fields.get(key) != "disabled"]
    flows[chat_id] = {"step": "dynamic_amount", "direction": direction, "method": method, "fields": fields, "remaining_fields": ordered, "values": {}}
    minimum = method.get("minimum_amount") or (20000 if direction == "withdraw" else 0)
    label = "رصيد البوت" if direction == "withdraw" else method.get("payment_currency", "المبلغ")
    details = [f"💳 <b>{html.escape(str(method['display_name']))}</b>"]
    if method.get("account_name"):
        details.append(f"الاسم: <b>{html.escape(str(method['account_name']))}</b>")
    if method.get("account_identifier"):
        details.append(f"الحساب: <code>{html.escape(str(method['account_identifier']))}</code>")
    if method.get("network"):
        details.append(f"الشبكة: <b>{html.escape(str(method['network']))}</b>")
    if method.get("instructions"):
        details.append(html.escape(str(method["instructions"])))
    if method.get("asset_storage_key"):
        try:
            details.append(f'<a href="{html.escape(payment_storage().presign(method["asset_storage_key"], 300))}">عرض صورة الطريقة</a>')
        except Exception:
            pass
    details.append(f"\n💰 أدخل المبلغ ({label}).\n🔻 الحد الأدنى: <b>{minimum}</b>")
    return set_panel(chat_id, "\n".join(details), inline([nav_row("home", "🔙 إلغاء")]))


def show_ichancy_deposit(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        return set_panel(chat_id, (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        ), inline([[cb("🎮 إنشاء حساب iChancy", "ichancy_create")], nav_row("home")]))
    flows[chat_id] = {"step": "ichancy_deposit_amount", "operation_ref": str(uuid.uuid4())}
    save_persistent_flow(chat_id, flows[chat_id])
    return set_panel(chat_id, (
        "🎮 <b>شحن حساب iChancy</b>\n\n"
        f"💰 رصيدك المتاح: <b>{fmt_amount(u['balance'])}</b>\n"
        f"🔻 الحد الأدنى للشحن: <b>{fmt_amount(MIN_ICHANCY_DEPOSIT)}</b> من رصيد البوت = <b>{fmt_amount(MIN_ICHANCY_DEPOSIT * ICHANCY_UNITS_PER_BOT_UNIT)}</b> iChancy\n"
        "🔁 كل 100 من رصيد البوت = 10,000 iChancy\n\n"
        "أدخل المبلغ الذي ترغب بإضافته إلى حسابك."
    ), inline([nav_row("home", "🔙 إلغاء")]))


def show_ichancy_withdraw(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        return set_panel(chat_id, (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        ), inline([[cb("🎮 إنشاء حساب iChancy", "ichancy_create")], nav_row("home")]))
    flows[chat_id] = {"step": "ichancy_withdraw_amount", "operation_ref": str(uuid.uuid4())}
    save_persistent_flow(chat_id, flows[chat_id])
    return set_panel(chat_id, (
        "↩️ <b>سحب من حساب iChancy</b>\n\n"
        f"🔻 الحد الأدنى للسحب: <b>{fmt_amount(MIN_ICHANCY_WITHDRAW)}</b> iChancy\n"
        "🔁 التحويل: كل 10,000 iChancy = 100 من رصيد البوت\n\n"
        "أدخل مبلغ iChancy المراد سحبه، ويجب أن يكون من مضاعفات 100."
    ), inline([nav_row("home", "🔙 إلغاء")]))


def show_history(chat_id):
    return set_panel(chat_id, "📋 <b>سجل العمليات</b>\n\nاختر نوع العمليات:", inline([
        [cb("⬇️ شحن رصيد البوت", "history:bot_topup:0")],
        [cb("⬆️ سحب رصيد البوت", "history:bot_withdraw:0")],
        [cb("🎮 شحن iChancy", "history:ichancy_deposit:0")],
        [cb("↩️ سحب من iChancy", "history:ichancy_withdraw:0")],
        [cb("📋 جميع العمليات", "history:all:0")],
        nav_row("home"),
    ]))


def _history_types(filter_name):
    return {
        "bot_topup": ["bot_topup"],
        "bot_withdraw": ["bot_withdraw"],
        "ichancy_deposit": ["ichancy_deposit"],
        "ichancy_withdraw": ["ichancy_withdraw"],
    }.get(filter_name, [])


def _history_title(filter_name):
    return {
        "bot_topup": "⬇️ شحن رصيد البوت",
        "bot_withdraw": "⬆️ سحب رصيد البوت",
        "ichancy_deposit": "🎮 شحن iChancy",
        "ichancy_withdraw": "↩️ سحب من iChancy",
        "all": "📋 جميع العمليات",
    }.get(filter_name, "📋 جميع العمليات")


def _local_time(value):
    if not value:
        return "-"
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        if ZoneInfo:
            value = value.astimezone(ZoneInfo("Asia/Damascus"))
        else:
            value = value.astimezone(timezone(timedelta(hours=3)))
        return value.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return str(value)[:16]


def _amount_prefix(item):
    tx_type = item.get("tx_type")
    if tx_type in {"bot_topup", "ichancy_withdraw", "gift", "referral"}:
        return "+"
    if tx_type in {"bot_withdraw", "ichancy_deposit"}:
        return "-"
    return ""


def show_history_page(chat_id, filter_name="all", page=0):
    page = max(0, int(page))
    per_page = 10
    items = list_transactions(chat_id, per_page + 1, page * per_page, _history_types(filter_name))
    has_next = len(items) > per_page
    items = items[:per_page]

    labels = {
        "test_credit": "🧪 تعديل رصيد تجريبي",
        "bot_topup": "⬇️ شحن رصيد البوت",
        "bot_withdraw": "⬆️ سحب رصيد البوت",
        "ichancy_deposit": "🎮 شحن iChancy",
        "ichancy_withdraw": "↩️ سحب من iChancy",
        "gift": "🎟️ كود هدية",
        "referral": "👥 مكافأة إحالة",
    }
    statuses = {
        "completed": "✅ مكتملة",
        "pending": "⏳ قيد المعالجة",
        "approved": "🟡 مقبولة",
        "rejected": "❌ مرفوضة",
        "refunded": "↩️ تم إرجاع الرصيد",
        "failed": "❌ فشلت",
    }

    lines = [_history_title(filter_name), ""]
    if not items:
        lines.append("لا توجد عمليات مسجلة.")
    else:
        for idx, item in enumerate(items):
            lines.append(labels.get(item.get("tx_type"), "🧾 عملية"))
            lines.append(f"💰 <b>{_amount_prefix(item)}{fmt_amount(item.get('amount', 0))}</b>")
            if item.get("method"):
                lines.append(f"💳 {html.escape(str(item['method']))}")
            lines.append(statuses.get(item.get("status"), html.escape(str(item.get("status", "")))))
            if item.get("tx_code"):
                lines.append(f"🧾 <code>{html.escape(str(item['tx_code']))}</code>")
            lines.append(f"🕒 {_local_time(item.get('created_at'))}")
            if idx != len(items) - 1:
                lines.append("────────────")

    rows = []
    paging = []
    if page > 0:
        paging.append(cb("⬅️ السابق", f"history:{filter_name}:{page-1}"))
    if has_next:
        paging.append(cb("التالي ➡️", f"history:{filter_name}:{page+1}"))
    if paging:
        rows.append(paging)
    rows.append([cb("📋 أنواع العمليات", "history")])
    rows.append(nav_row("home"))
    return set_panel(chat_id, "\n".join(lines), inline(rows))


def show_offers(chat_id):
    b = get_bonuses()
    return set_panel(chat_id, (
        "🎁 <b>العروض والبونصات الحالية</b>\n\n"
        f"💸 Sham Cash: <b>+{b['sham']}%</b>\n"
        f"🔴 Syriatel Cash: <b>+{b['syriatel']}%</b>\n"
        f"🟢 USDT: <b>+{b['usdt']}%</b>\n"
        f"🟣 Wish Money: <b>+{b['wish']}%</b>\n\n"
        "استفد من البونص عند شحن رصيد البوت عبر الوسيلة التي عليها عرض."
    ), inline([[cb("⬇️ شحن رصيد البوت", "topup")], nav_row("home")]))


def show_referrals(chat_id, bot_username=None):
    bot_username = bot_username or os.environ.get("BOT_USERNAME", "YourBot")
    link = f"https://t.me/{bot_username}?start=ref_{chat_id}"
    count, earnings = referral_stats(chat_id)
    return set_panel(chat_id, (
        "👥 <b>نظام الإحالات</b>\n\n"
        "شارك رابطك الخاص وادعُ أصدقاءك للانضمام إلى البوت.\n\n"
        f"👤 عدد الأشخاص المسجلين عن طريقك: <b>{count}</b>\n"
        f"💰 أرباح الإحالات: <b>{fmt_amount(earnings)}</b>\n\n"
        f"🔗 رابط الإحالة الخاص بك:\n<code>{html.escape(link)}</code>"
    ), inline([[copy_btn("📋 نسخ رابط الإحالة", link)], nav_row("home")]))


def show_gift(chat_id):
    flows[chat_id] = {"step": "gift_code"}
    return set_panel(chat_id, (
        "🎟️ <b>كود الهدية</b>\n\n"
        "أدخل كود الهدية للاستفادة من المكافأة.\n\n"
        "كل كود صالح لاستخدام واحد فقط على مستوى البوت بالكامل."
    ), inline([nav_row("home", "🔙 إلغاء")]))


def support_usernames():
    result = db_support_usernames()
    if result:
        return result
    raw = os.environ.get("SUPPORT_USERNAMES", "")
    return [
        x.strip().lstrip("@")
        for x in raw.split(",")
        if re.fullmatch(r"[A-Za-z0-9_]{5,32}", x.strip().lstrip("@"))
    ]


def show_support(chat_id):
    names = support_usernames()
    text = "👨‍💻 <b>تواصل مع خدمة العملاء</b>"
    rows = []
    if names:
        for name in names:
            rows.append([url_btn(f"👨‍💻 @{name}", f"https://t.me/{name}")])
    else:
        text += "\n\nلم تتم إضافة ممثل خدمة عملاء بعد."
    rows.append(nav_row("home"))
    return set_panel(chat_id, text, inline(rows))


def show_terms(chat_id):
    return set_panel(chat_id, (
        "📜 <b>الشروط والخدمات</b>\n\n"
        "يُرجى قراءة الشروط بعناية قبل استخدام الخدمات.\n\n"
        "• تأكد من صحة بيانات حساب iChancy قبل تنفيذ أي عملية.\n"
        "• طلبات سحب رصيد البوت تخضع لمراجعة الإدارة قبل التنفيذ.\n"
        "• بعد تقديم طلب السحب، تتم معالجة الحوالة بأسرع وقت ممكن، وعادةً خلال ساعة إلى 3 ساعات.\n"
        "• يجب إدخال بيانات وسيلة الاستلام بشكل صحيح.\n"
        "• يحق للإدارة مراجعة أي عملية مشبوهة لحماية الحسابات والأرصدة.\n"
        "• نسب البونص والعروض قابلة للتغيير وتظهر داخل قسم شحن رصيد البوت.\n\n"
        "👑 شكرًا لثقتكم بخدماتنا"
    ), inline([nav_row("home")]))


# -----------------------------------------------------------------------------
# Text-flow handlers
# -----------------------------------------------------------------------------
def parse_amount(text):
    cleaned = text.replace(",", "").replace(" ", "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if value > 0 else None


def parse_decimal_amount(text):
    cleaned = text.replace(",", "").replace(" ", "").strip()
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0 or value.as_tuple().exponent < -2:
        return None
    return value


def process_text_input(chat_id, text):
    flow = flows.get(chat_id)
    if not flow:
        flow = load_persistent_flow(chat_id)
        if flow:
            flows[chat_id] = flow
    if not flow:
        return False

    step = flow.get("step")
    u = get_user(chat_id)

    if step == "dynamic_amount":
        try:
            amount = parse_decimal_amount(text)
            if amount is None:
                raise ValueError("invalid_amount")
            flow["values"]["amount"] = str(amount)
            flow["step"] = "dynamic_field"
            return bool(_dynamic_prompt_next(chat_id))
        except ValueError:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا ضمن الحدود المسموحة.", inline([nav_row("home", "🔙 إلغاء")]))
            return True

    if step == "dynamic_field":
        key = flow.get("current_field")
        if key == "proof_image":
            set_panel(chat_id, "🧾 أرسل صورة إثبات التحويل.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        value = text.strip()
        if not value and (flow.get("fields") or {}).get(key) == "required":
            set_panel(chat_id, "⚠️ هذا الحقل مطلوب.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        if value:
            flow["values"][key] = value
        flow["step"] = "dynamic_field"
        return bool(_dynamic_prompt_next(chat_id))

    if step == "ichancy_username":
        candidate = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,24}", candidate):
            set_panel(chat_id, "👤 <b>اكتب اسم المستخدم</b>\n\nاسم المستخدم غير صالح.", inline([nav_row("account", "🔙 إلغاء")]))
            return True
        if username_taken(candidate, chat_id):
            set_panel(chat_id, "👤 <b>اكتب اسم المستخدم</b>\n\n❌ اسم المستخدم مستخدم من قبل، اختر اسمًا آخر.", inline([nav_row("account", "🔙 إلغاء")]))
            return True
        flow["username"] = candidate
        flow["step"] = "ichancy_password"
        save_persistent_flow(chat_id, flow)
        set_panel(chat_id, "🔐 <b>اكتب كلمة المرور</b>", inline([nav_row("account", "🔙 إلغاء")]))
        return True

    if step == "ichancy_password":
        password = text.strip()
        if len(password) < 6 or len(password) > 64:
            set_panel(chat_id, "🔐 <b>اكتب كلمة المرور</b>\n\nكلمة المرور قصيرة جدًا.", inline([nav_row("account", "🔙 إلغاء")]))
            return True
        ok, reason = save_ichancy_credentials(chat_id, flow["username"], password)
        if not ok:
            flow["step"] = "ichancy_username"
            save_persistent_flow(chat_id, flow)
            set_panel(chat_id, "👤 <b>اكتب اسم المستخدم</b>\n\n❌ اسم المستخدم مستخدم من قبل، اختر اسمًا آخر.", inline([nav_row("account", "🔙 إلغاء")]))
            return True
        password_visible[chat_id] = False
        flows.pop(chat_id, None)
        delete_persistent_flow(chat_id)
        if registration_mode() == "manual":
            registration = create_manual_registration_request(chat_id, flow["username"], password)
        else:
            registration = enqueue_player_registration(chat_id, flow["username"], password)
        if registration.get("ok"):
            if registration_mode() == "manual":
                set_panel(chat_id, registration_status_message("admin_pending"), inline([nav_row("home")]))
            else:
                set_panel(chat_id, registration_status_message("pending"), inline([nav_row("home")]))
        elif registration.get("reason") == "already_pending":
            show_account(chat_id)
        else:
            set_panel(chat_id, "⚠️ <b>تعذر بدء إنشاء الحساب حاليًا.</b>\n\nلم يتم إنشاء أي حساب في iChancy. حاول لاحقًا.", inline([[cb("🔁 إعادة المحاولة", "ichancy_create")], nav_row("account")]))
        return True

    if step == "sham_syp_amount":
        amount = parse_amount(text)
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل المبلغ بالأرقام فقط.", inline([nav_row("topup_sham_syp", "🔙 إلغاء")]))
            return True
        if amount < MIN_TOPUP_SYP:
            set_panel(chat_id, (
                f"❌ <b>الحد الأدنى للإيداع هو {fmt_amount(MIN_TOPUP_SYP)} ل.س</b>\n\n"
                "أدخل مبلغًا أعلى."
            ), inline([nav_row("topup_sham_syp", "🔙 إلغاء")]))
            return True
        flow["cash_amount"] = amount
        flow["step"] = "sham_syp_reference"
        set_panel(chat_id, (
            "🧾 <b>اكتب رقم العملية</b>\n\n"
            f"💰 المبلغ: <b>{fmt_amount(amount)} ل.س</b>\n\n"
            "الصق رقم العملية / المرجع الموجود في Sham Cash بعد التحويل."
        ), inline([nav_row("topup_sham_syp", "🔙 إلغاء")]))
        return True

    if step == "sham_syp_reference":
        operation_ref = text.strip()
        if len(operation_ref) < 3 or len(operation_ref) > 120:
            set_panel(chat_id, "⚠️ رقم العملية غير صالح. أرسله كما يظهر في Sham Cash.", inline([nav_row("topup_sham_syp", "🔙 إلغاء")]))
            return True

        result = create_sham_syp_topup_request(chat_id, flow["cash_amount"], operation_ref)
        if not result.get("ok"):
            if result.get("reason") == "duplicate_ref":
                msg = "❌ <b>رقم العملية مستخدم مسبقًا.</b>\n\nتأكد من رقم العملية وحاول مرة ثانية."
            else:
                msg = "⚠️ <b>تعذر حفظ طلب الإيداع حاليًا.</b>\n\nحاول مرة ثانية بعد قليل."
            set_panel(chat_id, msg, inline([nav_row("topup_sham_syp")]))
            return True

        flows.pop(chat_id, None)
        bonus_line = ""
        if result["bonus_percent"] > 0:
            bonus_line = (
                f"🎁 البونص: <b>+{result['bonus_percent']}%</b> "
                f"({fmt_amount(result['bonus_amount'])})\n"
            )
        set_panel(chat_id, (
            "✅ <b>تم استلام طلب الإيداع</b>\n\n"
            f"💵 المبلغ المحول: <b>{fmt_amount(result['cash_amount'])} ل.س</b>\n"
            f"💰 الرصيد الأساسي: <b>{fmt_amount(result['base_bot'])}</b>\n"
            f"{bonus_line}"
            f"💳 الرصيد المتوقع بعد التحقق: <b>{fmt_amount(result['total_bot'])}</b>\n"
            f"🧾 رقم الطلب: <code>{html.escape(result['request_code'])}</code>\n\n"
            "⏳ طلبك قيد المراجعة. سيتم إضافة الرصيد بعد التحقق من الحوالة."
        ), inline([nav_row("home")]))
        return True

    if step == "bot_topup_amount":
        currency = flow.get("currency")
        method = flow.get("method", "غير محدد")
        back_data = flow.get("back_data", "topup")
        if currency == "SYP":
            amount = parse_amount(text)
            minimum = int(Decimal(flow.get("minimum", str(MIN_TOPUP_SYP))))
            if amount is None:
                set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row(back_data, "🔙 إلغاء")]))
                return True
            if amount < minimum:
                set_panel(chat_id, f"❌ <b>الحد الأدنى للإيداع هو {fmt_amount(minimum)} ل.س</b>\n\nأدخل مبلغًا أعلى.", inline([nav_row(back_data, "🔙 إلغاء")]))
                return True
            flows.pop(chat_id, None)
            show_topup_ready(chat_id, method, currency, Decimal(amount))
            return True

        amount = parse_decimal_amount(text)
        minimum = Decimal(flow.get("minimum", str(MIN_TOPUP_USD)))
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا، مثال: <code>2</code> أو <code>2.5</code>.", inline([nav_row(back_data, "🔙 إلغاء")]))
            return True
        if amount < minimum:
            set_panel(chat_id, f"❌ <b>الحد الأدنى للإيداع هو ${minimum}</b>\n\nأدخل مبلغًا أعلى.", inline([nav_row(back_data, "🔙 إلغاء")]))
            return True
        flows.pop(chat_id, None)
        show_topup_ready(chat_id, method, currency, amount)
        return True

    if step == "withdraw_bot_amount":
        amount = parse_amount(text)
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row("withdraw_bot", "🔙 إلغاء")]))
            return True
        if amount < MIN_BOT_WITHDRAW:
            set_panel(chat_id, f"❌ <b>الحد الأدنى للسحب هو {fmt_amount(MIN_BOT_WITHDRAW)}</b>\n\nأدخل مبلغًا أعلى.", inline([nav_row("withdraw_bot", "🔙 إلغاء")]))
            return True
        if amount > u["balance"]:
            flows.pop(chat_id, None)
            set_panel(chat_id, (
                "⚠️ <b>الرصيد غير كافٍ</b>\n\n"
                f"رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
                f"المبلغ المطلوب: <b>{fmt_amount(amount)}</b>"
            ), inline([nav_row("withdraw_bot")]))
            return True
        method = flow.get("method", "غير محدد")
        flows.pop(chat_id, None)
        set_panel(chat_id, (
            "✅ <b>تم استلام طلب السحب</b>\n\n"
            f"💰 المبلغ: <b>{fmt_amount(amount)}</b>\n"
            f"💳 طريقة الاستلام: <b>{html.escape(method)}</b>\n\n"
            "⏱️ سيتم تنفيذ طلبك بأسرع وقت ممكن، وعادةً خلال مدة تتراوح بين ساعة و3 ساعات."
        ), inline([nav_row("home")]))
        return True

    if step == "ichancy_deposit_amount":
        amount = parse_amount(text)
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        if amount < MIN_ICHANCY_DEPOSIT:
            set_panel(chat_id, f"❌ <b>الحد الأدنى لشحن iChancy هو {fmt_amount(MIN_ICHANCY_DEPOSIT)}</b>\n\nأدخل مبلغًا أعلى.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        flows.pop(chat_id, None)
        if amount > u["balance"]:
            set_panel(chat_id, (
                "⚠️ <b>الرصيد غير كافٍ</b>\n\n"
                f"رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
                f"المبلغ المطلوب: <b>{fmt_amount(amount)}</b>"
            ), inline([nav_row("home")]))
            return True
        result = enqueue_ichancy_deposit(chat_id, amount, flow.get("operation_ref"))
        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "player_not_mapped":
                msg = "⚠️ <b>حساب iChancy غير مربوط بعد.</b>\n\nأنشئ الحساب واربط Player ID أولًا."
            elif reason == "insufficient_balance":
                msg = "⚠️ <b>الرصيد غير كافٍ.</b>\n\nتعذر حجز طلب الشحن من رصيدك الحالي."
            else:
                msg = "⚠️ <b>تعذر إرسال طلب الشحن للجسر.</b>\n\nحاول مرة أخرى بعد قليل."
            set_panel(chat_id, msg, inline([nav_row("home")]))
            return True
        set_panel(chat_id, (
            "⏳ <b>جارٍ شحن حساب iChancy</b>\n\n"
            f"💰 من رصيد البوت: <b>{fmt_amount(amount)}</b>\n"
            f"🎮 إلى iChancy: <b>{fmt_amount(result['ichancy_amount'])}</b>\n"
            f"🧾 رقم الطلب: <code>{html.escape(result['request_id'])}</code>\n\n"
            "تم إرسال الطلب إلى الهاتف الوسيط. لن يُخصم المبلغ من رصيدك إلا بعد تأكيد iChancy للعملية."
        ), inline([nav_row("home")]))
        return True

    if step == "ichancy_withdraw_amount":
        ichancy_amount = parse_amount(text)
        if ichancy_amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        if ichancy_amount < MIN_ICHANCY_WITHDRAW:
            set_panel(chat_id, f"❌ <b>الحد الأدنى للسحب هو {fmt_amount(MIN_ICHANCY_WITHDRAW)} iChancy</b>", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        result = enqueue_ichancy_withdraw(chat_id, ichancy_amount, flow.get("operation_ref"))
        flows.pop(chat_id, None)
        if not result.get("ok"):
            reason = result.get("reason")
            msg = "⚠️ المبلغ يجب أن يكون من مضاعفات 100." if reason == "invalid_conversion" else "⚠️ تعذر إرسال طلب السحب للجسر."
            set_panel(chat_id, msg, inline([nav_row("home")]))
            return True
        set_panel(chat_id, (
            "⏳ <b>جارٍ سحب الرصيد من iChancy</b>\n\n"
            f"🎮 من iChancy: <b>{fmt_amount(result['ichancy_amount'])}</b>\n"
            f"💰 سيضاف إلى رصيد البوت: <b>{fmt_amount(result['bot_amount'])}</b>\n"
            f"🧾 رقم الطلب: <code>{html.escape(result['request_id'])}</code>\n\n"
            "لن تتم إضافة الرصيد إلى البوت إلا بعد تأكيد نجاح العملية من iChancy."
        ), inline([nav_row("home")]))
        return True

    if step == "gift_code":
        flows.pop(chat_id, None)
        result = redeem_gift_code(chat_id, text)
        if result.get("ok"):
            set_panel(chat_id, (
                "🎉 <b>تم استخدام كود الهدية بنجاح</b>\n\n"
                f"💰 تمت إضافة: <b>{fmt_amount(result['amount'])}</b>\n"
                f"💳 رصيدك الحالي: <b>{fmt_amount(result['after'])}</b>\n"
                f"🧾 <code>{html.escape(result['tx_code'])}</code>"
            ), inline([nav_row("home")]))
            return True
        reason = result.get("reason")
        if reason == "used":
            msg = "❌ <b>تم استخدام كود الهدية مسبقًا.</b>"
        elif reason == "storage_unavailable":
            msg = "⚠️ <b>الخدمة غير متاحة مؤقتًا.</b>"
        else:
            msg = "❌ <b>كود الهدية غير صالح أو غير موجود.</b>"
        set_panel(chat_id, msg, inline([nav_row("home")]))
        return True

    return False


def handle_menu_text(chat_id, text):
    mapping = {
        "🎮 حساب iChancy 🎮": show_account,
        "⬇️ شحن رصيد البوت": show_topup,
        "⬆️ سحب رصيد البوت": show_withdraw_bot,
        "🎮 شحن حساب iChancy": show_ichancy_deposit,
        "💸 سحب من حساب iChancy": show_ichancy_withdraw,
        "📋 سجل العمليات": show_history,
        "🎁 العروض والبونصات": show_offers,
        "👥 نظام الإحالات": show_referrals,
        "🎟️ كود الهدية": show_gift,
        "💬 الدعم والمساعدة": show_support,
        "📜 الشروط والخدمات": show_terms,
    }
    fn = mapping.get(text)
    if fn:
        fn(chat_id)
        return True
    return False


# -----------------------------------------------------------------------------
# Callback dispatcher
# -----------------------------------------------------------------------------
def handle_callback(query):
    callback_id = query.get("id")
    data = query.get("data", "")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return

    if message.get("message_id"):
        panel_message_ids[chat_id] = message["message_id"]
        panel_message_kinds[chat_id] = "photo" if message.get("photo") else "text"

    answer_callback(callback_id)

    if data == "home":
        flows.pop(chat_id, None)
        set_panel(chat_id, greeting(chat_id), main_inline_keyboard())
    elif data == "account":
        show_account(chat_id)
    elif data == "ichancy_create":
        flows[chat_id] = {"step": "ichancy_username"}
        save_persistent_flow(chat_id, flows[chat_id])
        set_panel(chat_id, "👤 <b>اكتب اسم المستخدم</b>", inline([nav_row("account", "🔙 إلغاء")]))
    elif data == "ichancy_toggle_password":
        password_visible[chat_id] = not password_visible.get(chat_id, False)
        show_account(chat_id)
    elif data == "topup":
        flows.pop(chat_id, None)
        show_topup(chat_id)
    elif data.startswith("dyn_dep:"):
        _start_dynamic_payment(chat_id, data.split(":", 1)[1], "deposit")
    elif data.startswith("dyn_wd:"):
        _start_dynamic_payment(chat_id, data.split(":", 1)[1], "withdraw")
    elif data.startswith("dyn_skip:"):
        flow = flows.get(chat_id) or {}
        if flow.get("step") == "dynamic_field" and flow.get("current_field"):
            flow["values"].pop(flow["current_field"], None)
            _dynamic_prompt_next(chat_id)
    elif data == "topup_sham":
        show_sham_cash_currency(chat_id)
    elif data == "topup_sham_syp":
        flows.pop(chat_id, None)
        show_sham_syp_payment(chat_id)
    elif data == "sham_syp_done":
        flows[chat_id] = {"step": "sham_syp_amount"}
        set_panel(chat_id, (
            "💰 <b>اكتب المبلغ الذي حولته</b>\n\n"
            f"🔻 الحد الأدنى: <b>{fmt_amount(MIN_TOPUP_SYP)} ل.س</b>"
        ), inline([nav_row("topup_sham_syp", "🔙 إلغاء")]))
    elif data == "topup_sham_usd":
        show_topup_amount_prompt(chat_id, "Sham Cash Dollar", "USD", MIN_TOPUP_USD, "topup_sham")
    elif data == "topup_syriatel":
        show_topup_amount_prompt(chat_id, "Syriatel Cash", "SYP", MIN_TOPUP_SYP, "topup")
    elif data == "topup_wish":
        show_topup_amount_prompt(chat_id, "Wish Money", "USD", MIN_TOPUP_USD, "topup")
    elif data == "topup_usdt":
        show_usdt_networks(chat_id)
    elif data == "usdt_trc20":
        show_topup_amount_prompt(chat_id, "USDT - TRC20", "USD", MIN_TOPUP_USD, "topup_usdt")
    elif data == "usdt_bep20":
        show_topup_amount_prompt(chat_id, "USDT - BEP20", "USD", MIN_TOPUP_USD, "topup_usdt")
    elif data == "withdraw_bot":
        flows.pop(chat_id, None)
        show_withdraw_bot(chat_id)
    elif data == "wd_sham":
        show_withdraw_method(chat_id, "Sham Cash")
    elif data == "wd_syriatel":
        show_withdraw_method(chat_id, "Syriatel Cash")
    elif data == "wd_usdt":
        show_withdraw_method(chat_id, "USDT")
    elif data == "wd_wish":
        show_withdraw_method(chat_id, "Wish Money")
    elif data == "ichancy_deposit":
        show_ichancy_deposit(chat_id)
    elif data == "ichancy_withdraw":
        show_ichancy_withdraw(chat_id)
    elif data == "ichancy_balance":
        result = enqueue_ichancy_balance(chat_id)
        if result.get("ok"):
            set_panel(chat_id, "⏳ <b>جارٍ تحديث الرصيد من iChancy…</b>\n\nستصلك النتيجة تلقائيًا بعد تأكيد الجسر.", inline([nav_row("account")]))
        else:
            set_panel(chat_id, "⚠️ <b>تعذر طلب الرصيد حاليًا.</b>\n\nتأكد من ربط حساب iChancy ثم أعد المحاولة.", inline([nav_row("account")]))
    elif data == "history":
        show_history(chat_id)
    elif data.startswith("history:"):
        parts = data.split(":")
        filter_name = parts[1] if len(parts) > 1 else "all"
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        show_history_page(chat_id, filter_name, page)
    elif data == "offers":
        show_offers(chat_id)
    elif data == "referrals":
        show_referrals(chat_id)
    elif data == "gift":
        show_gift(chat_id)
    elif data == "support":
        show_support(chat_id)
    elif data == "terms":
        show_terms(chat_id)
    else:
        answer_callback(callback_id, "هذا الخيار قيد التجهيز", True)


# -----------------------------------------------------------------------------
# Bridge device API
# -----------------------------------------------------------------------------
def bridge_authorized(req):
    supplied = req.headers.get("X-Bridge-Key", "")
    return bool(BRIDGE_SHARED_SECRET) and hmac.compare_digest(supplied, BRIDGE_SHARED_SECRET)
def admin_bridge_authorized(req):
    supplied = req.headers.get("X-Admin-Bridge-Key", "")
    return bool(ADMIN_BRIDGE_SHARED_SECRET) and hmac.compare_digest(supplied, ADMIN_BRIDGE_SHARED_SECRET)


def financial_admin_actor(req):
    """Return the authenticated service identity; caller-supplied actor headers are not trusted."""
    if not admin_bridge_authorized(req):
        return None
    return {"actor_id": FINANCIAL_ADMIN_SERVICE_ID, "actor_type": "service_account"}


def _financial_payload_hash(body):
    canonical = json.dumps(body or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _claim_financial_idempotency(cur, operation_type, cash_request_id, key, actor_id, payload_hash):
    key = str(key or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", key):
        raise ValueError("invalid_idempotency_key")
    cur.execute("""
        SELECT idempotency_key,payload_hash,response_status,response_body,actor_id
        FROM financial_idempotency
        WHERE operation_type=%s AND cash_request_id=%s AND idempotency_key=%s
        FOR UPDATE
    """, (operation_type, cash_request_id, key))
    existing = cur.fetchone()
    if existing:
        if existing["payload_hash"] != payload_hash:
            raise ValueError("idempotency_key_payload_mismatch")
        if existing["response_body"] is not None:
            return existing["response_status"], dict(existing["response_body"]), True
        return None, None, False
    cur.execute("""
        INSERT INTO financial_idempotency(operation_type,cash_request_id,idempotency_key,actor_id,payload_hash)
        VALUES(%s,%s,%s,%s,%s)
    """, (operation_type, cash_request_id, key, actor_id, payload_hash))
    return None, None, False


def _finish_financial_idempotency(cur, operation_type, cash_request_id, key, status_code, body):
    cur.execute("""
        UPDATE financial_idempotency
        SET response_status=%s,response_body=%s,updated_at=NOW()
        WHERE operation_type=%s AND cash_request_id=%s AND idempotency_key=%s
          AND response_body IS NULL
    """, (int(status_code), Json(body), operation_type, cash_request_id, key))
    if cur.rowcount != 1:
        raise RuntimeError("financial_idempotency_write_failed")
def bridge_json_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


def payment_storage():
    global _PAYMENT_STORAGE
    if _PAYMENT_STORAGE is None:
        _PAYMENT_STORAGE = storage_from_environment()
    return _PAYMENT_STORAGE


def dynamic_payment_methods(direction):
    if not db_enabled():
        return []
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT m.id AS method_id,m.display_name,d.*,COALESCE((SELECT jsonb_object_agg(f.field_key,f.required_mode) FROM payment_method_fields f WHERE f.payment_method_direction_id=d.id),'{}'::jsonb) AS fields,a.id AS asset_id,a.storage_key AS asset_storage_key
                   FROM payment_methods m JOIN payment_method_directions d ON d.payment_method_id=m.id AND d.direction=%s AND d.enabled=TRUE
                   LEFT JOIN payment_assets a ON a.payment_method_id=m.id AND a.is_active=TRUE
                   WHERE m.enabled=TRUE AND m.archived_at IS NULL ORDER BY m.sort_order,m.id""",
                (direction,),
            )
            return [dict(row) for row in cur.fetchall()]


def _dynamic_snapshot(method, direction):
    keys = ("method_id", "display_name", "direction", "payment_currency", "reference_currency", "network", "account_name", "account_identifier", "instructions", "exchange_rate", "rate_definition", "conversion_multiplier", "fee_type", "fee_value", "bonus_enabled", "bonus_percent", "minimum_amount", "maximum_amount", "amount_scale", "asset_id")
    snapshot = {key: method.get(key) for key in keys}
    snapshot["direction"] = direction
    snapshot["fields"] = method.get("fields") or {}
    snapshot["method"] = method.get("display_name")
    snapshot["currency"] = method.get("payment_currency")
    snapshot["account_details"] = {
        "name": method.get("account_name"),
        "identifier": method.get("account_identifier"),
    }
    snapshot["fees"] = {"type": method.get("fee_type"), "value": method.get("fee_value")}
    snapshot["bonus"] = {"enabled": method.get("bonus_enabled"), "percent": method.get("bonus_percent")}
    snapshot["min"] = method.get("minimum_amount")
    snapshot["max"] = method.get("maximum_amount")
    snapshot["decimal_places"] = method.get("amount_scale")
    return snapshot


def create_dynamic_cash_request(chat_id, method, direction, values, attachment_content=None):
    """Create request, snapshot, optional attachment and withdrawal reservation atomically."""
    fields = method.get("fields") or {}
    validate_dynamic_fields(fields, values)
    request_values = dict(values)
    operation_ref = None
    if direction == "deposit" and fields.get("operation_reference") != "disabled":
        operation_ref = normalize_operation_reference(request_values.get("operation_reference"))
        request_values["operation_reference"] = operation_ref
    snapshot = _dynamic_snapshot(method, direction)
    quote = calculate_deposit_quote(request_values["amount"], snapshot) if direction == "deposit" else calculate_withdraw_quote(request_values["amount"], snapshot)
    amount_bot = int(quote["bot_amount"] if direction == "deposit" else quote["input_bot_amount"])
    request_type = "topup" if direction == "deposit" else "withdraw"
    request_code = f"PAY-{uuid.uuid4().hex[:16].upper()}"
    amount_original = Decimal(str(request_values["amount"]))

    def json_safe(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {k: json_safe(v) for k, v in value.items()}
        return value
    storage_key = None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO cash_requests(request_code,telegram_id,request_type,amount_bot,amount_cash,method,status,details,payment_method_id,payment_direction,amount_original,conversion_snapshot)
                       VALUES(%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s) RETURNING id""",
                    (request_code, int(chat_id), request_type, amount_bot, int(amount_original), method["display_name"], Json(json_safe({"fields": request_values, **({"operation_ref": operation_ref} if operation_ref else {}), "quote": quote})), method["method_id"], direction, str(amount_original), Json(json_safe(snapshot))),
                )
                request_id = cur.fetchone()["id"]
                snapshot_id = create_method_snapshot(cur, request_id, method["method_id"], method["id"], method.get("asset_id"), Json(json_safe(snapshot)))
                cur.execute("UPDATE cash_requests SET method_snapshot_id=%s WHERE id=%s", (snapshot_id, request_id))
                if attachment_content is not None:
                    asset = validate_image(attachment_content)
                    storage_key = f"cash-requests/{request_id}/{uuid.uuid4().hex}"
                    payment_storage().upload(storage_key, asset.content, asset.content_type)
                    cur.execute("""INSERT INTO cash_request_attachments(cash_request_id,storage_key,content_type,file_size,sha256)
                                   VALUES(%s,%s,%s,%s,%s)""", (request_id, storage_key, asset.content_type, asset.size, asset.sha256))
                if direction == "withdraw":
                    reserve_withdrawal(cur, request_id, int(chat_id), amount_bot)
        return {"ok": True, "request_code": request_code, "quote": quote, "request_id": request_id}
    except Exception:
        if storage_key:
            try:
                payment_storage().delete(storage_key)
            except Exception:
                pass
        raise


def enqueue_bridge_job(request_id, job_type, payload):
    """Create one outbound job; request_id makes financial commands idempotent."""
    if not db_enabled():
        raise RuntimeError("postgres_required")
    ensure_db()
    job_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bridge_jobs(job_id, request_id, job_type, payload)
                VALUES(%s,%s,%s,%s)
                ON CONFLICT(request_id) DO NOTHING
                RETURNING job_id, request_id, status
            """, (job_id, request_id, job_type, Json(payload or {})))
            row = cur.fetchone()
            if row:
                return dict(row)
            cur.execute("SELECT job_id, request_id, status FROM bridge_jobs WHERE request_id=%s", (request_id,))
            return dict(cur.fetchone())
def enqueue_admin_players_read(operation_ref):
    """Queue exactly one read-only get_players job for an authorized admin caller."""
    operation_ref = str(operation_ref or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_ref):
        return {"ok": False, "reason": "invalid_operation_ref"}
    if not db_enabled():
        return {"ok": False, "reason": "postgres_required"}
    job = enqueue_bridge_job(f"admin-get-players:{operation_ref}", "get_players", {
        "source": "admin_readonly",
        "operation_ref": operation_ref,
    })
    return {"ok": True, "job_id": str(job["job_id"]), "request_id": job["request_id"], "status": job["status"]}


def generated_player_email(username, chat_id):
    """Generate a non-customer-facing address required by the official API."""
    local = re.sub(r"[^a-z0-9]", "", str(username).lower())[:24]
    return f"{local}.{int(chat_id)}@{PLAYER_EMAIL_DOMAIN}"


def enqueue_player_registration(chat_id, username, password):
    """Queue one idempotent Player registration for the outbound phone Bridge."""
    if not db_enabled():
        return {"ok": False, "reason": "postgres_required"}
    ensure_db()
    user = get_user(chat_id)
    if user.get("ichancy_player_id"):
        return {"ok": False, "reason": "already_mapped"}
    if user.get("ichancy_creation_status") in {"pending", "running"}:
        return {"ok": False, "reason": "already_pending", "job_id": user.get("ichancy_creation_job_id")}

    payload = {
        "telegram_id": int(chat_id),
        "username": username,
        "email": generated_player_email(username, chat_id),
    }
    request_id = f"register-player:{chat_id}:{username.lower()}:{uuid.uuid4()}"
    job = enqueue_bridge_job(request_id, "register_player", payload)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET ichancy_creation_status='pending',
                    ichancy_creation_job_id=%s,
                    updated_at=NOW()
                WHERE telegram_id=%s
            """, (str(job["job_id"]), chat_id))
            cur.execute("""
                UPDATE ichancy_registration_requests
                SET status='queued', admin_note=NULL, updated_at=NOW()
                WHERE id = (
                    SELECT id FROM ichancy_registration_requests
                    WHERE telegram_id=%s AND LOWER(username)=LOWER(%s)
                    ORDER BY created_at DESC LIMIT 1
                )
            """, (int(chat_id), username))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO ichancy_registration_requests
                        (request_code, telegram_id, username, email, status)
                    VALUES(%s,%s,%s,%s,'queued')
                    ON CONFLICT (request_code) DO NOTHING
                """, (
                    f"JOB-{str(job['job_id'])}", int(chat_id), username,
                    payload["email"],
                ))
    return {"ok": True, "job_id": str(job["job_id"])}


def enqueue_ichancy_deposit(chat_id, amount, operation_ref=None):
    """Queue an idempotent iChancy deposit for the authenticated phone Bridge."""
    if not db_enabled():
        return {"ok": False, "reason": "postgres_required"}
    ensure_db()
    user = get_user(chat_id)
    player_id = str(user.get("ichancy_player_id") or "").strip()
    if not player_id:
        return {"ok": False, "reason": "player_not_mapped"}
    amount = int(amount)
    if amount <= 0 or amount > int(user.get("balance") or 0):
        return {"ok": False, "reason": "insufficient_balance"}
    currency = str(user.get("ichancy_currency") or "USD").strip() or "USD"
    operation_ref = str(operation_ref or "").strip()
    if not operation_ref:
        operation_ref = str(uuid.uuid4())
    request_id = f"ichancy-deposit:{chat_id}:{operation_ref}"
    ichancy_amount = ichancy_deposit_units(amount)
    payload = {
        "telegram_id": int(chat_id),
        "player_id": player_id,
        "amount": ichancy_amount,
        "bot_amount": amount,
        "currency_code": currency,
        "comment": f"Telegram deposit {chat_id}",
    }
    job = enqueue_bridge_job(request_id, "deposit", payload)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM transactions
                WHERE tx_type='ichancy_deposit' AND details->>'job_id'=%s
                LIMIT 1
            """, (str(job["job_id"]),))
            if cur.fetchone():
                return {"ok": True, "job_id": str(job["job_id"]), "request_id": request_id,
                        "bot_amount": amount, "ichancy_amount": ichancy_amount}
        add_transaction(
            chat_id, "ichancy_deposit", amount, "pending", "bridge",
            {"request_id": request_id, "job_id": str(job["job_id"]), "player_id": player_id,
             "bot_amount": amount, "ichancy_amount": ichancy_amount,
             "conversion_rate": ICHANCY_UNITS_PER_BOT_UNIT},
            None, None, conn=conn,
        )
    return {"ok": True, "job_id": str(job["job_id"]), "request_id": request_id,
            "bot_amount": amount, "ichancy_amount": ichancy_amount}


def enqueue_ichancy_balance(chat_id):
    """Queue a read-only balance request for the phone Bridge."""
    if not db_enabled():
        return {"ok": False, "reason": "postgres_required"}
    ensure_db()
    user = get_user(chat_id)
    player_id = str(user.get("ichancy_player_id") or "").strip()
    if not player_id:
        return {"ok": False, "reason": "player_not_mapped"}
    request_id = f"ichancy-balance:{chat_id}:{uuid.uuid4()}"
    job = enqueue_bridge_job(request_id, "get_balance", {
        "telegram_id": int(chat_id), "player_id": player_id,
        "currency_code": str(user.get("ichancy_currency") or "USD"),
    })
    return {"ok": True, "job_id": str(job["job_id"])}


def enqueue_ichancy_withdraw(chat_id, ichancy_amount, operation_ref=None):
    """Queue the legacy iChancy withdrawal through the guarded reservation path."""
    if not db_enabled():
        return {"ok": False, "reason": "postgres_required"}
    ensure_db()
    user = get_user(chat_id)
    player_id = str(user.get("ichancy_player_id") or "").strip()
    if not player_id:
        return {"ok": False, "reason": "player_not_mapped"}
    ichancy_amount = int(ichancy_amount)
    try:
        bot_amount = ichancy_withdraw_bot_units(ichancy_amount)
    except ValueError:
        return {"ok": False, "reason": "invalid_conversion"}
    currency = str(user.get("ichancy_currency") or "USD").strip() or "USD"
    operation_ref = str(operation_ref or "").strip()
    if not operation_ref:
        operation_ref = str(uuid.uuid4())
    request_id = f"ichancy-withdraw:{chat_id}:{operation_ref}"
    payload = {
        "telegram_id": int(chat_id), "player_id": player_id,
        "amount": ichancy_amount, "bot_amount": bot_amount,
        "currency_code": currency, "comment": f"Telegram withdrawal {chat_id}",
    }
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, status, details, reservation_id
                FROM cash_requests
                WHERE telegram_id=%s AND request_type='withdraw'
                  AND details->>'operation_ref'=%s
                LIMIT 1
            """, (int(chat_id), operation_ref))
            existing = cur.fetchone()
            if existing:
                existing_details = existing["details"] or {}
                existing_job_id = str(existing_details.get("job_id") or "").strip()
                if not existing_job_id:
                    return {"ok": False, "reason": "withdrawal_reconciliation_required"}
                return {"ok": True, "job_id": existing_job_id, "request_id": request_id,
                        "bot_amount": bot_amount, "ichancy_amount": ichancy_amount}

            request_code = make_tx_code("IWD")
            details = {
                "request_id": request_id, "operation_ref": operation_ref,
                "player_id": player_id, "ichancy_amount": ichancy_amount,
                "bot_amount": bot_amount, "conversion_rate": ICHANCY_UNITS_PER_BOT_UNIT,
            }
            cur.execute("""
                INSERT INTO cash_requests(
                    request_code,telegram_id,request_type,amount_bot,amount_cash,
                    method,status,details
                ) VALUES(%s,%s,'withdraw',%s,%s,'ichancy','pending',%s)
                RETURNING id
            """, (request_code, int(chat_id), bot_amount, ichancy_amount, Json(details)))
            cash_request_id = int(cur.fetchone()["id"])
            reservation_id = reserve_withdrawal(cur, cash_request_id, int(chat_id), bot_amount)
            payload["cash_request_id"] = cash_request_id
            job_id = str(uuid.uuid4())
            cur.execute("""
                INSERT INTO bridge_jobs(job_id,request_id,job_type,payload)
                VALUES(%s,%s,'withdraw',%s)
                RETURNING job_id,request_id,status
            """, (job_id, request_id, Json(payload)))
            job = dict(cur.fetchone())
            add_transaction(
                chat_id, "ichancy_withdraw", bot_amount, "pending", "bridge",
                {**details, "job_id": job_id, "cash_request_id": cash_request_id,
                 "reservation_id": reservation_id}, None, None, conn=conn,
            )
            cur.execute("""
                UPDATE cash_requests
                SET details=details || %s::jsonb, updated_at=NOW()
                WHERE id=%s
            """, (Json({"job_id": job_id}), cash_request_id))
    return {"ok": True, "job_id": str(job["job_id"]), "request_id": request_id,
            "bot_amount": bot_amount, "ichancy_amount": ichancy_amount}


@app.route("/bridge/v1/heartbeat", methods=["POST"])
def bridge_heartbeat():
    if not bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("device_id", "")).strip()
    device_name = str(body.get("device_name", device_id)).strip()[:120]
    if not device_id or not device_name:
        return bridge_json_error("device_id and device_name are required")
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bridge_jobs
                SET status='pending', claimed_by=NULL, updated_at=NOW()
                WHERE job_type='register_player'
                  AND status='running'
                  AND updated_at < NOW() - INTERVAL '75 seconds'
            """)
            cur.execute("""
                INSERT INTO bridge_devices(device_id, device_name, status, ichancy_connected, last_heartbeat, metadata)
                VALUES(%s,%s,'online',%s,NOW(),%s)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_name=EXCLUDED.device_name,
                    status='online',
                    ichancy_connected=EXCLUDED.ichancy_connected,
                    last_heartbeat=NOW(),
                    metadata=EXCLUDED.metadata,
                    updated_at=NOW()
            """, (device_id, device_name, bool(body.get("ichancy_connected")), Json(body.get("metadata") or {})))
    return jsonify({"ok": True, "device_id": device_id, "status": "online", "server_time": datetime.now(timezone.utc).isoformat()})


@app.route("/admin/v1/bridge/players", methods=["POST"])
def admin_bridge_players():
    """Create an admin-only, read-only get_players job; no financial types accepted."""
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    if set(body) - {"operation_ref"}:
        return bridge_json_error("only operation_ref is allowed")
    result = enqueue_admin_players_read(body.get("operation_ref"))
    if not result.get("ok"):
        status = 503 if result.get("reason") == "postgres_required" else 400
        return bridge_json_error(result["reason"], status)
    return jsonify(result), 202


@app.route("/admin/v1/financial/cash-requests/<int:cash_request_id>/decision", methods=["POST"])
def admin_financial_decision(cash_request_id):
    """Record an admin decision without treating it as external success."""
    actor = financial_admin_actor(request)
    if not actor:
        return bridge_json_error("unauthorized", 401)
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    body = request.get_json(silent=True) or {}
    if set(body) - {"decision", "reason", "idempotency_key", "operation_ref"}:
        return bridge_json_error("unsupported_fields")
    decision = str(body.get("decision", "")).strip().lower()
    reason = str(body.get("reason", "")).strip()
    idem_key = str(body.get("idempotency_key", "")).strip()
    if decision not in {"review", "approve", "reject", "start_payout", "confirm_deposit"}:
        return bridge_json_error("invalid_decision")
    if len(reason) < 3 or len(reason) > 1000:
        return bridge_json_error("reason_required")
    if decision == "confirm_deposit":
        return bridge_json_error("external_verification_required", 409)
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,request_type,status,telegram_id,reservation_id FROM cash_requests WHERE id=%s FOR UPDATE", (cash_request_id,))
            item = cur.fetchone()
            if not item:
                return bridge_json_error("cash_request_not_found", 404)
            # The key is scoped to the request and financial decision operation;
            # changing the decision with the same key must be rejected.
            operation_type = "admin_financial_decision"
            try:
                replay_status, replay_body, replay = _claim_financial_idempotency(
                    cur, operation_type, cash_request_id, idem_key, actor["actor_id"], _financial_payload_hash(body)
                )
            except ValueError as exc:
                error_code = _safe_public_error(exc, "financial_idempotency_rejected")
                return bridge_json_error(error_code, 409 if "mismatch" in error_code else 400)
            if replay:
                return jsonify(replay_body), int(replay_status)
            previous = item["status"]
            try:
                if decision == "review":
                    new_status = previous
                elif item["request_type"] == "topup":
                    if decision == "approve" and previous == "pending":
                        cur.execute("UPDATE cash_requests SET status='approved',updated_at=NOW() WHERE id=%s AND status='pending'", (cash_request_id,))
                        new_status = "approved"
                    elif decision == "reject" and previous in {"pending", "approved"}:
                        cur.execute("UPDATE cash_requests SET status='rejected',updated_at=NOW() WHERE id=%s AND status IN ('pending','approved')", (cash_request_id,))
                        new_status = "rejected"
                    else:
                        raise ValueError("invalid_topup_transition")
                else:
                    if decision == "approve":
                        approve_withdrawal(cur, cash_request_id)
                        new_status = "approved"
                    elif decision == "start_payout":
                        start_withdrawal_payout(cur, cash_request_id)
                        new_status = "payout_in_progress"
                    elif decision == "reject" and previous == "pending":
                        cur.execute("UPDATE cash_requests SET status='rejected',updated_at=NOW() WHERE id=%s AND status='pending'", (cash_request_id,))
                        new_status = "rejected"
                    else:
                        raise ValueError("invalid_withdraw_transition")
                record_financial_audit(cur, actor["actor_id"], actor["actor_type"], "admin_decision",
                                       cash_request_id, item["request_type"], previous, new_status,
                                       decision, reason, body.get("operation_ref"), None,
                                       {"idempotency_key": idem_key})
                response = {"ok": True, "cash_request_id": cash_request_id, "decision": decision,
                            "status": new_status, "external_outcome": "not_confirmed"}
                _finish_financial_idempotency(cur, operation_type, cash_request_id, idem_key, 200, response)
                return jsonify(response)
            except ValueError as exc:
                conn.rollback()
                return bridge_json_error(_safe_public_error(exc, "financial_transition_rejected"), 409)


@app.route("/admin/v1/financial/cash-requests/<int:cash_request_id>/external-outcome", methods=["POST"])
def admin_financial_external_outcome(cash_request_id):
    """Manual admin evidence cannot certify an external result."""
    if not financial_admin_actor(request):
        return bridge_json_error("unauthorized", 401)
    return bridge_json_error("external_verification_required", 409)


def _payment_admin_payload_valid(body, require_direction=False):
    """Strict validation for admin payment configuration; no production defaults."""
    if not isinstance(body, dict):
        return "invalid_json"
    if require_direction:
        direction = str(body.get("direction", "")).strip().lower()
        if direction not in {"deposit", "withdraw"}:
            return "invalid_direction"
        definition = str(body.get("rate_definition", "")).strip()
        if len(definition) < 8 or not any(x in definition.lower() for x in (" per ", " per", "/", "لكل", "مقابل")):
            return "rate_definition_must_describe_units"
        try:
            rate = Decimal(str(body.get("rate")))
            multiplier = Decimal(str(body.get("conversion_multiplier", 1)))
            minimum = Decimal(str(body.get("minimum_amount", 0)))
            maximum = body.get("maximum_amount")
            maximum = None if maximum in (None, "") else Decimal(str(maximum))
            fee = Decimal(str(body.get("fee_value", 0)))
            bonus = Decimal(str(body.get("bonus_percent", 0)))
        except (InvalidOperation, TypeError, ValueError):
            return "invalid_numeric_config"
        if rate <= 0 or multiplier <= 0 or minimum < 0 or (maximum is not None and maximum < minimum):
            return "invalid_limits_or_rate"
        fee_type = str(body.get("fee_type", "none")).lower()
        if fee < 0 or fee_type not in {"none", "fixed", "percent"} or (fee_type == "percent" and fee > 100):
            return "invalid_fee"
        if bonus < 0 or bonus > 1000:
            return "invalid_bonus"
        fields = body.get("fields", {}) or {}
        allowed_fields = {"amount", "operation_reference", "proof_image", "note", "recipient_account", "recipient_name", "network"}
        if not isinstance(fields, dict) or any(k not in allowed_fields or v not in {"required", "optional", "disabled"} for k, v in fields.items()):
            return "invalid_payment_fields"
    return None


def _payment_audit_safe(value):
    if not isinstance(value, dict):
        return {}
    clean = dict(value)
    for key in ("password", "token", "access_token", "refresh_token", "cookie", "secret"):
        if key in clean:
            clean[key] = "[redacted]"
    return clean


@app.route("/admin/v1/payment-methods", methods=["GET", "POST"])
def admin_payment_methods():
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    ensure_db()
    if request.method == "GET":
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT m.*, COALESCE(jsonb_agg(to_jsonb(d) ORDER BY d.direction)
                        FILTER (WHERE d.id IS NOT NULL), '[]'::jsonb) AS directions
                    FROM payment_methods m
                    LEFT JOIN payment_method_directions d ON d.payment_method_id=m.id
                    GROUP BY m.id ORDER BY m.sort_order,m.id
                """)
                return jsonify({"ok": True, "methods": [dict(row) for row in cur.fetchall()]})
    body = request.get_json(silent=True) or {}
    action = str(body.get("action", "")).strip().lower()
    if action == "create":
        key = str(body.get("method_key", "")).strip().lower()
        name = str(body.get("display_name", "")).strip()
        if not re.fullmatch(r"[a-z0-9_-]{2,80}", key) or not name:
            return bridge_json_error("invalid_method")
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO payment_methods(method_key,display_name) VALUES(%s,%s) RETURNING id", (key, name))
                method_id = cur.fetchone()["id"]
                audit("payment_method_create", method_id, {"method_key": key, "display_name": name}, conn=conn)
        return jsonify({"ok": True, "method_id": method_id}), 201
    return bridge_json_error("unsupported_action")


@app.route("/admin/v1/payment-methods/<int:method_id>", methods=["PATCH"])
def admin_payment_method_update(method_id):
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    body = request.get_json(silent=True) or {}
    if set(body) - {"display_name", "enabled", "archived"}:
        return bridge_json_error("unsupported_fields")
    if not any(key in body for key in ("display_name", "enabled", "archived")):
        return bridge_json_error("no_changes")
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_methods WHERE id=%s FOR UPDATE", (method_id,))
            old = cur.fetchone()
            if not old:
                return bridge_json_error("payment_method_not_found", 404)
            if "archived" in body and body["archived"]:
                cur.execute("UPDATE payment_methods SET archived_at=COALESCE(archived_at,NOW()),enabled=FALSE,updated_at=NOW() WHERE id=%s RETURNING *", (method_id,))
            else:
                cur.execute("UPDATE payment_methods SET display_name=COALESCE(%s,display_name),enabled=COALESCE(%s,enabled),updated_at=NOW() WHERE id=%s RETURNING *", (body.get("display_name"), body.get("enabled"), method_id))
            new = cur.fetchone()
            audit("payment_method_update", method_id, {"old": _payment_audit_safe(dict(old)), "new": _payment_audit_safe(dict(new))}, conn=conn)
            return jsonify({"ok": True, "method": dict(new)})


@app.route("/admin/v1/payment-methods/<int:method_id>/directions", methods=["PUT"])
def admin_payment_method_direction(method_id):
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    invalid = _payment_admin_payload_valid(body, require_direction=True)
    if invalid:
        return bridge_json_error(invalid)
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,archived_at FROM payment_methods WHERE id=%s FOR UPDATE", (method_id,))
            method = cur.fetchone()
            if not method or method["archived_at"]:
                return bridge_json_error("payment_method_not_found_or_archived", 404)
            cur.execute("SELECT * FROM payment_method_directions WHERE payment_method_id=%s AND direction=%s", (method_id, body["direction"]))
            old = cur.fetchone()
            cur.execute("""
                INSERT INTO payment_method_directions(payment_method_id,direction,payment_currency,reference_currency,network,account_name,account_identifier,instructions,exchange_rate,rate_definition,conversion_multiplier,fee_type,fee_value,bonus_enabled,bonus_percent,minimum_amount,maximum_amount,amount_scale)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(payment_method_id,direction) DO UPDATE SET payment_currency=EXCLUDED.payment_currency,reference_currency=EXCLUDED.reference_currency,network=EXCLUDED.network,account_name=EXCLUDED.account_name,account_identifier=EXCLUDED.account_identifier,instructions=EXCLUDED.instructions,exchange_rate=EXCLUDED.exchange_rate,rate_definition=EXCLUDED.rate_definition,conversion_multiplier=EXCLUDED.conversion_multiplier,fee_type=EXCLUDED.fee_type,fee_value=EXCLUDED.fee_value,bonus_enabled=EXCLUDED.bonus_enabled,bonus_percent=EXCLUDED.bonus_percent,minimum_amount=EXCLUDED.minimum_amount,maximum_amount=EXCLUDED.maximum_amount,amount_scale=EXCLUDED.amount_scale,updated_at=NOW()
                RETURNING *
            """, (method_id,body["direction"],body.get("payment_currency",""),body.get("reference_currency",""),body.get("network"),body.get("account_name"),body.get("account_identifier"),body.get("instructions"),body["rate"],body["rate_definition"],body.get("conversion_multiplier",1),body.get("fee_type","none"),body.get("fee_value",0),bool(body.get("bonus_enabled",False)),body.get("bonus_percent",0),body.get("minimum_amount",0),body.get("maximum_amount") or None,int(body.get("decimal_places",body.get("amount_scale",0)))))
            new = cur.fetchone()
            cur.execute("DELETE FROM payment_method_fields WHERE payment_method_direction_id=%s", (new["id"],))
            for field_key, mode in (body.get("fields") or {}).items():
                cur.execute("INSERT INTO payment_method_fields(payment_method_direction_id,field_key,required_mode) VALUES(%s,%s,%s)", (new["id"],field_key,mode))
            audit("payment_method_direction_update", method_id, {"direction": body["direction"], "old": _payment_audit_safe(dict(old) if old else {}), "new": _payment_audit_safe(dict(new))}, conn=conn)
            return jsonify({"ok": True, "direction": dict(new)})


@app.route("/admin/v1/payment-methods/<int:method_id>/asset", methods=["GET", "POST", "DELETE"])
def admin_payment_method_asset(method_id):
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    ensure_db()
    if request.method == "GET":
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id,storage_key,content_type,file_size,sha256,retention_status FROM payment_assets WHERE payment_method_id=%s AND is_active=TRUE ORDER BY created_at DESC LIMIT 1", (method_id,))
                asset = cur.fetchone()
                if not asset:
                    return bridge_json_error("payment_asset_not_found", 404)
                try:
                    url = payment_storage().presign(asset["storage_key"], 300)
                except Exception:
                    return bridge_json_error("private_storage_unavailable", 503)
                return jsonify({"ok": True, "asset": {**dict(asset), "temporary_url": url, "expires_seconds": 300}})
    confirm = str(request.form.get("confirm", "")).lower() in {"1", "true", "yes", "replace", "delete"}
    if request.method == "DELETE":
        if not confirm:
            return bridge_json_error("confirmation_required")
        with db_conn() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("SELECT id FROM payment_assets WHERE payment_method_id=%s AND is_active=TRUE ORDER BY created_at DESC LIMIT 1", (method_id,))
                    asset = cur.fetchone()
                    if not asset:
                        return bridge_json_error("payment_asset_not_found", 404)
                    result = mark_asset_deleted(cur, payment_storage(), asset["id"])
                    audit("payment_asset_delete", method_id, {"asset_id": asset["id"], "retention_status": result["status"]}, conn=conn)
                    return jsonify({"ok": True, "asset": {"asset_id": result["asset_id"], "status": result["status"]}})
                except Exception:
                    conn.rollback()
                    return bridge_json_error("payment_asset_delete_failed", 500)
    image = request.files.get("image")
    if not image:
        return bridge_json_error("image_required")
    if request.content_length and request.content_length > MAX_ASSET_BYTES + 256 * 1024:
        return bridge_json_error("image_too_large", 413)
    content = image.read()
    if len(content) > MAX_ASSET_BYTES:
        return bridge_json_error("image_too_large", 413)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM payment_assets WHERE payment_method_id=%s AND is_active=TRUE ORDER BY created_at DESC LIMIT 1", (method_id,))
            old = cur.fetchone()
            if old and not confirm:
                return bridge_json_error("replacement_confirmation_required", 409)
            new_key = None
            try:
                new_asset_id, new_key, validated = upload_payment_asset(cur, payment_storage(), method_id, content)
                if old:
                    retired = retire_asset(cur, payment_storage(), old["id"])
                    audit("payment_asset_replace", method_id, {"old_asset_id": old["id"], "old_status": retired["status"], "new_asset_id": new_asset_id, "sha256": validated.sha256}, conn=conn)
                else:
                    audit("payment_asset_upload", method_id, {"asset_id": new_asset_id, "sha256": validated.sha256, "content_type": validated.content_type}, conn=conn)
                return jsonify({"ok": True, "asset": {"asset_id": new_asset_id, "content_type": validated.content_type, "file_size": validated.size, "sha256": validated.sha256}}), 201
            except ValueError as exc:
                conn.rollback()
                return bridge_json_error(_safe_public_error(exc, "payment_asset_invalid"), 400)
            except Exception:
                conn.rollback()
                if new_key:
                    try:
                        payment_storage().delete(new_key)
                    except Exception:
                        pass
                return bridge_json_error("payment_asset_upload_failed", 500)


@app.route("/bridge/v1/jobs/next", methods=["POST"])
def bridge_next_job():
    if not bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("device_id", "")).strip()
    requested_types = body.get("job_types") or []
    if not isinstance(requested_types, list):
        return bridge_json_error("job_types must be an array")
    requested_types = [str(item) for item in requested_types if str(item) in {"register_player", "get_players", "get_balance", "deposit", "withdraw"}]
    if not device_id or not db_enabled():
        return bridge_json_error("device_id and postgres are required", 503 if not db_enabled() else 400)
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM bridge_devices
                WHERE device_id=%s AND last_heartbeat > NOW() - INTERVAL '2 minutes'
            """, (device_id,))
            if not cur.fetchone():
                return bridge_json_error("device_not_registered_or_stale", 409)
            # Never requeue an abandoned financial request: its external outcome
            # is unknown and automatic replay could duplicate the operation.
            cur.execute("""
                UPDATE bridge_jobs
                SET status='unknown_external_result', reconciliation_required=TRUE,
                    result=COALESCE(result, '{}'::jsonb) || jsonb_build_object(
                        'external_result_state', 'unknown',
                        'stage', 'bridge_disconnected_before_completion',
                        'retry_blocked', TRUE
                    ), updated_at=NOW()
                WHERE status='running'
                  AND job_type IN ('deposit','withdraw')
                  AND updated_at < NOW() - INTERVAL '2 minutes'
            """)
            cur.execute("""
                UPDATE transactions t
                SET status='unknown_external_result',
                    details=details || jsonb_build_object('reconciliation_required', TRUE)
                WHERE t.status='pending'
                  AND t.details->>'job_id' IN (
                      SELECT job_id::text FROM bridge_jobs
                      WHERE status='unknown_external_result'
                        AND job_type IN ('deposit','withdraw')
                        AND reconciliation_required=TRUE
                  )
            """)
            cur.execute("""
                UPDATE bridge_jobs SET status='pending', claimed_by=NULL, updated_at=NOW()
                WHERE status='running' AND job_type NOT IN ('deposit','withdraw')
                  AND updated_at < NOW() - INTERVAL '2 minutes'
            """)
            cur.execute("""
                UPDATE ichancy_registration_requests r
                SET status='queued', updated_at=NOW()
                WHERE r.status='running'
                  AND EXISTS (
                      SELECT 1 FROM bridge_jobs j
                      WHERE j.job_type='register_player'
                        AND j.status='pending'
                        AND (j.payload->>'telegram_id')::bigint=r.telegram_id
                        AND LOWER(j.payload->>'username')=LOWER(r.username)
                  )
            """)
            # A successful iChancy registration returns only result=1. Older app
            # versions searched using the login, while the player list exposes the
            # generated e-mail as username. Re-run the newest affected job once to
            # recover its Player ID without issuing another register request.
            cur.execute("""
                WITH newest_failed_registration AS (
                    SELECT DISTINCT ON ((payload->>'telegram_id')) job_id
                    FROM bridge_jobs
                    WHERE job_type='register_player'
                      AND status='failed'
                      AND attempts=1
                      AND error IN ('player_id_not_found_after_registration', 'player_id_recovery_not_found', 'registration_request_failed')
                    ORDER BY (payload->>'telegram_id'), finished_at DESC
                )
                UPDATE bridge_jobs
                SET status='pending', claimed_by=NULL, error=NULL, updated_at=NOW()
                WHERE job_id IN (SELECT job_id FROM newest_failed_registration)
            """)
            if requested_types:
                cur.execute("""
                    SELECT job_id::text AS job_id, request_id, job_type, payload, attempts,
                           execution_attempt_id::text AS execution_attempt_id
                    FROM bridge_jobs
                    WHERE status='pending' AND job_type = ANY(%s)
                      AND NOT (job_type='withdraw' AND NOT (payload ? 'cash_request_id'))
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """, (requested_types,))
            else:
                cur.execute("""
                    SELECT job_id::text AS job_id, request_id, job_type, payload, attempts,
                           execution_attempt_id::text AS execution_attempt_id
                    FROM bridge_jobs
                    WHERE status='pending'
                      AND NOT (job_type='withdraw' AND NOT (payload ? 'cash_request_id'))
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """)
            row = cur.fetchone()
            if not row:
                legacy_filter = "job_type = ANY(%s)" if requested_types else "TRUE"
                params = (requested_types,) if requested_types else ()
                cur.execute(
                    f"SELECT 1 FROM bridge_jobs WHERE status='pending' AND {legacy_filter} "
                    "AND job_type='withdraw' AND NOT (payload ? 'cash_request_id') LIMIT 1",
                    params,
                )
                if cur.fetchone():
                    return bridge_json_error("withdrawal_reconciliation_required", 409)
                return jsonify({"ok": True, "job": None})
            if row["job_type"] == "withdraw" and not int((row.get("payload") or {}).get("cash_request_id") or 0):
                return bridge_json_error("withdrawal_reconciliation_required", 409)
            if row["job_type"] in {"deposit", "withdraw"}:
                if row["job_type"] == "withdraw":
                    cash_request_id = int((row.get("payload") or {}).get("cash_request_id") or 0)
                    cur.execute("""
                        SELECT cr.status AS request_status,
                               cr.reservation_id,
                               br.status AS reservation_status,
                               br.external_outcome
                        FROM cash_requests cr
                        LEFT JOIN balance_reservations br
                          ON br.id=cr.reservation_id AND br.cash_request_id=cr.id
                        WHERE cr.id=%s AND cr.request_type='withdraw'
                        FOR UPDATE OF cr
                    """, (cash_request_id,))
                    withdrawal = cur.fetchone()
                    if not withdrawal:
                        return bridge_json_error("withdraw_request_not_found", 409)
                    if withdrawal["request_status"] != "approved":
                        return bridge_json_error("withdrawal_admin_approval_required", 409)
                    if (not withdrawal["reservation_id"]
                            or withdrawal["reservation_status"] != "held"
                            or withdrawal["external_outcome"] != "unknown_external_result"):
                        return bridge_json_error("withdrawal_reservation_not_held", 409)
                    cur.execute("""
                        SELECT 1
                        FROM bridge_jobs
                        WHERE job_id<>%s
                          AND job_type='withdraw'
                          AND status IN ('pending','running')
                          AND payload->>'cash_request_id'=%s
                        LIMIT 1
                    """, (row["job_id"], str(cash_request_id)))
                    if cur.fetchone():
                        return bridge_json_error("withdrawal_job_already_active", 409)
                attempt_id = str(uuid.uuid4())
                cur.execute("""
                    UPDATE bridge_jobs
                    SET status='running', attempts=attempts+1, claimed_by=%s,
                        execution_attempt_id=%s,
                        external_attempt_started_at=NOW(),
                        external_attempt_count=external_attempt_count+1,
                        reconciliation_required=FALSE,
                        started_at=COALESCE(started_at,NOW()), updated_at=NOW()
                    WHERE job_id=%s AND status='pending'
                    RETURNING execution_attempt_id::text AS execution_attempt_id
                """, (device_id, attempt_id, row["job_id"]))
                claimed = cur.fetchone()
                if not claimed:
                    return jsonify({"ok": True, "job": None})
                row["execution_attempt_id"] = claimed["execution_attempt_id"]
                if row["job_type"] == "withdraw":
                    start_withdrawal_payout(cur, cash_request_id)
            else:
                cur.execute("""
                    UPDATE bridge_jobs
                    SET status='running', attempts=attempts+1, claimed_by=%s,
                        started_at=COALESCE(started_at,NOW()), updated_at=NOW()
                    WHERE job_id=%s AND status='pending'
                """, (device_id, row["job_id"]))
            if row["job_type"] == "register_player":
                payload = row.get("payload") or {}
                cur.execute("""
                    UPDATE ichancy_registration_requests
                    SET status='running', updated_at=NOW()
                    WHERE id = (
                        SELECT id FROM ichancy_registration_requests
                        WHERE telegram_id=%s AND LOWER(username)=LOWER(%s)
                        ORDER BY created_at DESC LIMIT 1
                    )
                """, (payload.get("telegram_id"), payload.get("username", "")))
    return jsonify({"ok": True, "job": dict(row)})


@app.route("/bridge/v1/jobs/<job_id>/complete", methods=["POST"])
def bridge_complete_job(job_id):
    if not bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("device_id", "")).strip()
    status = body.get("status")
    if status not in {"succeeded", "failed", "unknown_external_result"}:
        return bridge_json_error("status must be succeeded, failed, or unknown_external_result")
    execution_attempt_id = str(body.get("execution_attempt_id") or "").strip()
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    ensure_db()
    completed_job = None
    accepted_completion = False
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT job_type,payload FROM bridge_jobs WHERE job_id=%s FOR UPDATE", (job_id,))
            existing_job = cur.fetchone()
            if not existing_job:
                return bridge_json_error("job_not_found", 404)
            if existing_job["job_type"] == "withdraw" and not int((existing_job["payload"] or {}).get("cash_request_id") or 0):
                return bridge_json_error("withdrawal_reconciliation_required", 409)
            if execution_attempt_id:
                completion_sql = """
                    UPDATE bridge_jobs
                    SET status=%s, result=%s, error=%s,
                        reconciliation_required=(%s='unknown_external_result'),
                        finished_at=NOW(), updated_at=NOW()
                    WHERE job_id=%s
                      AND job_type IN ('deposit','withdraw')
                      AND status IN ('running','unknown_external_result')
                      AND claimed_by=%s
                      AND execution_attempt_id=%s
                    RETURNING job_id::text AS job_id, request_id, status
                """
                completion_params = (status, Json(body.get("result") or {}), str(body.get("error", ""))[:500] or None,
                                     status, job_id, device_id, execution_attempt_id)
            else:
                completion_sql = """
                    UPDATE bridge_jobs
                    SET status=%s, result=%s, error=%s, finished_at=NOW(), updated_at=NOW()
                    WHERE job_id=%s AND status='running' AND claimed_by=%s
                      AND execution_attempt_id IS NULL
                    RETURNING job_id::text AS job_id, request_id, status
                """
                completion_params = (status, Json(body.get("result") or {}), str(body.get("error", ""))[:500] or None, job_id, device_id)
            cur.execute(completion_sql, completion_params)
            row = cur.fetchone()
            accepted_completion = bool(row)
            if not row:
                cur.execute("SELECT job_id::text AS job_id, request_id, status FROM bridge_jobs WHERE job_id=%s", (job_id,))
                row = cur.fetchone()
                if not row:
                    return bridge_json_error("job_not_found", 404)
            cur.execute("SELECT job_type, payload, result FROM bridge_jobs WHERE job_id=%s", (job_id,))
            completed_job = cur.fetchone()
            if accepted_completion and completed_job and completed_job.get("job_type") == "register_player":
                payload = completed_job.get("payload") or {}
                telegram_id = payload.get("telegram_id")
                result = completed_job.get("result") or {}
                player_id = str(result.get("player_id") or "").strip()
                if telegram_id and status == "succeeded" and player_id:
                    cur.execute("""
                        UPDATE users
                        SET ichancy_player_id=%s,
                            ichancy_username=COALESCE(%s, ichancy_username),
                            ichancy_currency=COALESCE(%s, ichancy_currency),
                            ichancy_creation_status='succeeded',
                            updated_at=NOW()
                        WHERE telegram_id=%s
                    """, (player_id, payload.get("username"), result.get("currency"), int(telegram_id)))
                    cur.execute("""
                        UPDATE ichancy_registration_requests
                        SET status='succeeded', player_id=%s, admin_note=NULL, updated_at=NOW()
                        WHERE telegram_id=%s AND LOWER(username)=LOWER(%s)
                          AND status IN ('queued','running','pending')
                    """, (player_id, int(telegram_id), payload.get("username", "")))
                elif telegram_id:
                    cur.execute("""
                        UPDATE users
                        SET ichancy_creation_status='failed', updated_at=NOW()
                        WHERE telegram_id=%s
                    """, (int(telegram_id),))
                    cur.execute("""
                        UPDATE ichancy_registration_requests
                        SET status='failed', admin_note=%s, updated_at=NOW()
                        WHERE telegram_id=%s AND LOWER(username)=LOWER(%s)
                          AND status IN ('queued','running','pending')
                    """, (str(body.get("error") or "registration_failed")[:240], int(telegram_id), payload.get("username", "")))
            if accepted_completion and completed_job and completed_job.get("job_type") == "get_balance":
                # Read-only result: never mutate the bot balance or financial ledger.
                pass
            if accepted_completion and completed_job and completed_job.get("job_type") in {"deposit", "withdraw"}:
                payload = completed_job.get("payload") or {}
                telegram_id = payload.get("telegram_id")
                kind = completed_job.get("job_type")
                tx_type = "ichancy_deposit" if kind == "deposit" else "ichancy_withdraw"
                amount = int(payload.get("bot_amount") or 0)
                if telegram_id:
                    cur.execute("SAVEPOINT financial_settlement")
                    try:
                        if kind == "withdraw":
                            cash_request_id = int(payload.get("cash_request_id") or 0)
                            if not cash_request_id:
                                raise RuntimeError("withdrawal_reconciliation_required")
                            if status == "unknown_external_result":
                                mark_withdrawal_unknown(cur, cash_request_id, str(body.get("error") or "external outcome unknown"))
                                tx_final_status = "unknown_external_result"
                            elif status == "succeeded":
                                external_reference = str((body.get("result") or {}).get("external_reference") or "").strip() or None
                                record_external_outcome(cur, cash_request_id, "external_confirmed_success", external_reference)
                                settle_withdrawal(cur, cash_request_id, external_reference)
                                tx_final_status = "completed"
                            else:
                                record_external_outcome(cur, cash_request_id, "external_confirmed_failure")
                                release_withdrawal(cur, cash_request_id, "failed")
                                tx_final_status = "failed"
                            cur.execute("""
                                UPDATE transactions SET status=%s
                                WHERE telegram_id=%s AND tx_type='ichancy_withdraw'
                                  AND details->>'job_id'=%s
                                  AND status IN ('pending','unknown_external_result')
                            """, (tx_final_status, int(telegram_id), str(job_id)))
                            if cur.rowcount != 1:
                                raise RuntimeError("withdrawal_transaction_rowcount")
                        elif status == "unknown_external_result":
                            cur.execute("""
                                UPDATE transactions SET status='unknown_external_result',
                                    details=details || jsonb_build_object('reconciliation_required', TRUE)
                                WHERE telegram_id=%s AND tx_type=%s
                                  AND details->>'job_id'=%s
                                  AND status IN ('pending','unknown_external_result')
                            """, (int(telegram_id), tx_type, str(job_id)))
                            if cur.rowcount != 1:
                                raise RuntimeError(f"{kind}_unknown_transaction_rowcount")
                        elif status == "succeeded" and amount > 0:
                            cur.execute("SELECT balance FROM users WHERE telegram_id=%s FOR UPDATE", (int(telegram_id),))
                            user = cur.fetchone()
                            if not user:
                                raise RuntimeError(f"{kind}_user_missing")
                            before = int(user["balance"] or 0)
                            if kind == "deposit" and before < amount:
                                raise RuntimeError("deposit_local_balance_insufficient")
                            after = before - amount if kind == "deposit" else before + amount
                            cur.execute("UPDATE users SET balance=%s,updated_at=NOW() WHERE telegram_id=%s", (after, int(telegram_id)))
                            if cur.rowcount != 1:
                                raise RuntimeError(f"{kind}_balance_rowcount")
                            cur.execute("""
                                UPDATE transactions SET status='completed', balance_before=%s, balance_after=%s
                                WHERE telegram_id=%s AND tx_type=%s
                                  AND details->>'job_id'=%s AND status IN ('pending','unknown_external_result')
                            """, (before, after, int(telegram_id), tx_type, str(job_id)))
                            if cur.rowcount != 1:
                                raise RuntimeError(f"{kind}_completed_transaction_rowcount")
                        else:
                            cur.execute("""
                                UPDATE transactions SET status='failed'
                                WHERE telegram_id=%s AND tx_type=%s
                                  AND details->>'job_id'=%s AND status IN ('pending','unknown_external_result')
                            """, (int(telegram_id), tx_type, str(job_id)))
                            if cur.rowcount != 1:
                                raise RuntimeError(f"{kind}_rejected_transaction_rowcount")
                        cur.execute("RELEASE SAVEPOINT financial_settlement")
                    except Exception as settlement_error:
                        cur.execute("ROLLBACK TO SAVEPOINT financial_settlement")
                        if status == "succeeded":
                            trace_id = _safe_exception_metadata("financial.settlement", settlement_error)
                            reconciliation_error = f"external_succeeded_local_settlement_failed:trace={trace_id}"[:500]
                            cur.execute("""
                                UPDATE bridge_jobs
                                SET status='external_succeeded_reconciliation_required',
                                    reconciliation_required=TRUE, error=%s, updated_at=NOW()
                                WHERE job_id=%s AND status='succeeded'
                            """, (reconciliation_error, job_id))
                            cur.execute("""
                                UPDATE transactions SET status='external_succeeded_reconciliation_required',
                                    details=details || jsonb_build_object('reconciliation_required', TRUE, 'external_status', 'succeeded')
                                WHERE telegram_id=%s AND tx_type=%s
                                  AND details->>'job_id'=%s
                                  AND status IN ('pending','unknown_external_result')
                            """, (int(telegram_id), tx_type, str(job_id)))
                            if cur.rowcount != 1:
                                raise RuntimeError(f"{kind}_reconciliation_transaction_rowcount") from settlement_error
                        else:
                            raise
    if accepted_completion and completed_job and completed_job.get("job_type") == "register_player":
        payload = completed_job.get("payload") or {}
        chat_id = payload.get("telegram_id")
        result = completed_job.get("result") or {}
        if chat_id and status == "succeeded" and result.get("player_id"):
            set_panel(int(chat_id), "✅ <b>تم إنشاء حساب iChancy بنجاح</b>\n\nتم ربط الحساب ببياناتك. يمكنك فتح حساب iChancy لعرض اسم المستخدم وكلمة المرور وPlayer ID.", inline([nav_row("account", "🎮 فتح الحساب")]))
        elif chat_id:
            error = str(body.get("error") or "")
            if error == "username_taken":
                message = "⚠️ <b>اسم المستخدم مستخدم من قبل.</b>\n\nيرجى اختيار اسم مستخدم إنكليزي جديد مع أرقام، مثل: <code>Ahmad129</code>."
            elif error.startswith("register_player_http_"):
                message = "⚠️ <b>تعذر إنشاء حساب iChancy حاليًا.</b>\n\nلم يتم اعتماد الحساب، ولم تُعرض تفاصيل الاتصال الخارجية. أعد المحاولة لاحقًا."
            elif error == "registration_timeout_45s":
                message = "⚠️ <b>انتهت مهلة إنشاء الحساب.</b>\n\nلم يتم اعتماد الحساب. أعد المحاولة لاحقًا بعد التأكد من جاهزية الجسر."
            elif error.startswith("registration_runtime_error:"):
                message = "⚠️ <b>تعذر إنشاء حساب iChancy حاليًا.</b>\n\nلم يتم اعتماد الحساب، ولم تُعرض تفاصيل الاتصال الخارجية. أعد المحاولة لاحقًا."
            else:
                message = "⚠️ <b>لم يكتمل إنشاء حساب iChancy.</b>\n\nلم يتم إنشاء حساب جديد. اختر اسم مستخدم آخر أو أعد المحاولة لاحقًا."
            set_panel(int(chat_id), message, inline([[cb("🔁 اختيار اسم آخر", "ichancy_create")], nav_row("account")]))
    elif accepted_completion and completed_job and completed_job.get("job_type") == "get_balance":
        payload = completed_job.get("payload") or {}
        chat_id = payload.get("telegram_id")
        result = completed_job.get("result") or {}
        if chat_id and status == "succeeded":
            balance = html.escape(str(result.get("balance", "غير متاح")))
            currency = html.escape(str(result.get("currency") or payload.get("currency_code") or ""))
            set_panel(int(chat_id), f"✅ <b>تم تحديث الرصيد من iChancy</b>\n\n💰 الرصيد الحالي: <b>{balance} {currency}</b>", inline([nav_row("account")]))
        elif chat_id:
            set_panel(int(chat_id), _customer_job_outcome_message("get_balance", status), inline([nav_row("account")]))
    elif accepted_completion and completed_job and completed_job.get("job_type") == "deposit":
        payload = completed_job.get("payload") or {}
        chat_id = payload.get("telegram_id")
        if chat_id and status == "succeeded":
            set_panel(int(chat_id), "✅ <b>تم شحن حساب iChancy بنجاح</b>\n\nتم تأكيد العملية من iChancy وخصم المبلغ من رصيدك.", inline([nav_row("home")]))
        elif chat_id:
            set_panel(int(chat_id), _customer_job_outcome_message("deposit", status), inline([nav_row("home")]))
    elif accepted_completion and completed_job and completed_job.get("job_type") == "withdraw":
        payload = completed_job.get("payload") or {}
        chat_id = payload.get("telegram_id")
        if chat_id and status == "succeeded":
            set_panel(int(chat_id), "✅ <b>تم سحب الرصيد من iChancy بنجاح</b>\n\nتم تأكيد العملية وإضافة الرصيد المحول إلى رصيد البوت.", inline([nav_row("home")]))
        elif chat_id:
            set_panel(int(chat_id), _customer_job_outcome_message("withdraw", status), inline([nav_row("home")]))
    return jsonify({"ok": True, "job": dict(row)})


@app.route("/bridge/v1/jobs/<job_id>/credentials", methods=["POST"])
def bridge_job_credentials(job_id):
    """Return an encrypted-at-rest player password only to the device running this job."""
    if not bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("device_id", "")).strip()
    if not device_id or not db_enabled():
        return bridge_json_error("device_id and postgres are required", 503 if not db_enabled() else 400)
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT payload
                FROM bridge_jobs
                WHERE job_id=%s AND job_type='register_player'
                  AND status='running' AND claimed_by=%s
                FOR UPDATE
            """, (job_id, device_id))
            job = cur.fetchone()
            if not job:
                return bridge_json_error("job_not_claimed_by_device", 409)
            telegram_id = (job.get("payload") or {}).get("telegram_id")
            if not telegram_id:
                return bridge_json_error("job_payload_invalid", 422)
            cur.execute("SELECT ichancy_password_enc FROM users WHERE telegram_id=%s", (int(telegram_id),))
            user = cur.fetchone()
    password = decrypt_secret((user or {}).get("ichancy_password_enc"))
    if not password:
        return bridge_json_error("player_password_unavailable", 409)
    return jsonify({"ok": True, "password": password})


@app.route("/bridge/status", methods=["GET"])
def bridge_status():
    if not bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    if not db_enabled():
        return jsonify({"ok": True, "bridge": None, "reason": "postgres_required"})
    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT device_id, device_name,
                       CASE WHEN last_heartbeat > NOW() - INTERVAL '90 seconds' THEN 'online' ELSE 'offline' END AS status,
                       ichancy_connected, last_heartbeat, metadata
                FROM bridge_devices ORDER BY updated_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            cur.execute("SELECT status, COUNT(*) AS count FROM bridge_jobs GROUP BY status")
            jobs = {item["status"]: int(item["count"]) for item in cur.fetchall()}
    return jsonify({"ok": True, "bridge": dict(row) if row else None, "jobs": jobs})


# -----------------------------------------------------------------------------
# Flask routes
# -----------------------------------------------------------------------------
@app.before_request
def prepare_storage():
    if db_enabled():
        ensure_db()


@app.route("/")
def home():
    return "Al Asmar customer bot v18 is running ✅"


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "mode": "ui-prototype",
        "storage": "postgres" if db_enabled() else "memory",
        "ledger": "v18-sham-syp-qr-request",
        "sham_qr": os.path.exists(SHAM_SYP_QR_PATH),
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if received_secret != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    if update.get("callback_query"):
        query = update["callback_query"]
        started = time.perf_counter()
        try:
            handle_callback(query)
        except Exception as exc:
            _safe_exception_metadata("webhook.callback", exc)
            callback_id = query.get("id")
            if callback_id:
                answer_callback(callback_id, "⚠️ حدث خطأ مؤقت، حاول مرة ثانية.", True)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            print(f"callback handled=1 elapsed_ms={elapsed_ms}", flush=True)
        return jsonify({"ok": True})

    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text", "")
    photo = message.get("photo") or []
    message_id = message.get("message_id")
    if not chat_id:
        return jsonify({"ok": True})

    sender = message.get("from") or {}
    referrer_id = None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("ref_"):
            raw_ref = parts[1][4:]
            if raw_ref.isdigit():
                referrer_id = int(raw_ref)

    upsert_user(
        chat_id,
        telegram_username=sender.get("username"),
        first_name=sender.get("first_name"),
        referred_by=referrer_id,
    )

    if text.startswith("/start") or text.startswith("/menu"):
        flows.pop(chat_id, None)
        delete_persistent_flow(chat_id)
        show_home(chat_id, force_new=True)
        return jsonify({"ok": True})

    if text == "/myid":
        send_message(chat_id, f"🆔 معرفك على البوت: <code>{chat_id}</code>")
        return jsonify({"ok": True})

    try:
        active_flow = flows.get(chat_id) or load_persistent_flow(chat_id) or {}
        if photo and active_flow.get("step") == "dynamic_field" and active_flow.get("current_field") == "proof_image":
            try:
                active_flow["attachment_content"] = download_telegram_photo(photo)
                active_flow["values"]["proof_image"] = "attached"
                flows[chat_id] = active_flow
                _dynamic_prompt_next(chat_id)
            except Exception:
                set_panel(chat_id, "⚠️ تعذر استلام صورة الإثبات. أرسل صورة واضحة من فضلك.", inline([nav_row("home", "🔙 إلغاء")]))
            return jsonify({"ok": True})
        if process_text_input(chat_id, text):
            if message_id:
                delete_message(chat_id, message_id)
            return jsonify({"ok": True})

        if handle_menu_text(chat_id, text):
            if message_id:
                delete_message(chat_id, message_id)
            return jsonify({"ok": True})

        set_panel(chat_id, "👑 اختر الخدمة المطلوبة من القائمة.", main_inline_keyboard())
        if message_id:
            delete_message(chat_id, message_id)
    except Exception as exc:
        _safe_exception_metadata("webhook.message", exc)
        send_message(chat_id, "⚠️ حدث خطأ مؤقت، حاول مرة ثانية.")

    return jsonify({"ok": True})


@app.route("/diagnostics")
def diagnostics():
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    return jsonify({
        "ok": True,
        "version": "v18-sham-syp-qr-request",
        "db": "postgres" if db_enabled() else "memory",
        "sham_qr": os.path.exists(SHAM_SYP_QR_PATH),
        "telegram_queried": False,
    })


@app.route("/set-webhook")
def set_webhook():
    if not admin_bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    ensure_native_menu()
    response = tg("setWebhook", {
        "url": f"{PUBLIC_BASE_URL}/webhook",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
