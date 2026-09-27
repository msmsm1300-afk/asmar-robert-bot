import os
import re
import html
import json
import base64
import hashlib
import hmac
import secrets
import traceback
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

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or 0)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://asmar-robert-bot.onrender.com"
).rstrip("/")
BRIDGE_SHARED_SECRET = os.environ.get("BRIDGE_SHARED_SECRET", "").strip()
PLAYER_EMAIL_DOMAIN = os.environ.get("ICHANCY_PLAYER_EMAIL_DOMAIN", "asmarrobert.example").strip().lower()

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

# Transaction minimums agreed for the customer bot.
MIN_TOPUP_SYP = 20_000
MIN_TOPUP_USD = Decimal("2")
MIN_BOT_WITHDRAW = 20_000
MIN_ICHANCY_DEPOSIT = 20_000
MIN_ICHANCY_WITHDRAW = 50_000

# Cash-to-bot conversion: 1 SYP sent = 100 bot balance units.
SYP_TO_BOT_MULTIPLIER = 100

# Sham Cash SYP payment details.
SHAM_SYP_ACCOUNT_ID = "52b1612cc4685d57d0adb96d4d1be37a"
SHAM_SYP_ACCOUNT_NAME = "هشام محمد فتوح"
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
SHAM_SYP_QR_PATH = os.path.join(ASSET_DIR, "sham_cash_syp.jpg")


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
            # The same transfer reference cannot be submitted twice for the same method.
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_requests_topup_method_ref
                ON cash_requests(method, (details->>'operation_ref'))
                WHERE request_type='topup' AND details ? 'operation_ref'
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
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_bridge_jobs_pending
                ON bridge_jobs(status, created_at)
            """)
            for method, percent in DEFAULT_BONUSES.items():
                cur.execute(
                    "INSERT INTO bonuses(method, percent) VALUES(%s, %s) ON CONFLICT(method) DO NOTHING",
                    (method, percent),
                )
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
    operation_ref = operation_ref.strip()
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
                """, (
                    request_code,
                    chat_id,
                    total_bot,
                    int(cash_amount),
                    method,
                    SHAM_SYP_ACCOUNT_ID,
                    Json(details),
                ))

            tx_code = add_transaction(
                chat_id,
                "bot_topup",
                total_bot,
                "pending",
                method,
                {**details, "request_code": request_code, "cash_amount": int(cash_amount)},
                None,
                None,
                conn=conn,
            )
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


# -----------------------------------------------------------------------------
# Telegram helpers
# -----------------------------------------------------------------------------
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
            print(f"Telegram API error in {method}: {data}", flush=True)
        return data
    except Exception as exc:
        print(f"Telegram API exception in {method}: {exc}", flush=True)
        return {"ok": False, "description": str(exc)}


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
            print(f"Telegram API error in sendPhoto: {result}", flush=True)
        return result
    except Exception as exc:
        print(f"Telegram API exception in sendPhoto: {exc}", flush=True)
        return {"ok": False, "description": str(exc)}


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
    if not u["ichancy_player_id"] and u.get("ichancy_creation_status") in {"pending", "running"}:
        return set_panel(
            chat_id,
            "⏳ <b>جارٍ إنشاء حساب iChancy</b>\n\nتم إرسال طلبك إلى جهاز الوسيط. ستصلك رسالة تلقائيًا عند اكتمال الإنشاء. لا تعِد إرسال الطلب.",
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
    balance_line = ""
    if u.get("ichancy_player_id"):
        balance = fetch_ichancy_balance(chat_id)
        if balance.get("ok"):
            balance_line = f"\n💰 الرصيد الحقيقي: <b>{html.escape(str(balance['balance']))} {html.escape(str(balance.get('currency') or ''))}</b>\n"
        else:
            balance_line = "\n💰 الرصيد الحقيقي: <i>تعذر جلبه حاليًا، حاول لاحقًا</i>\n"
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
        [url_btn("🌐 الدخول إلى iChancy", "https://www.ichancy200.com")],
        nav_row("home"),
    ]))


def show_topup(chat_id):
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


def show_ichancy_deposit(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        return set_panel(chat_id, (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        ), inline([[cb("🎮 إنشاء حساب iChancy", "ichancy_create")], nav_row("home")]))
    flows[chat_id] = {"step": "ichancy_deposit_amount"}
    return set_panel(chat_id, (
        "🎮 <b>شحن حساب iChancy</b>\n\n"
        f"💰 رصيدك المتاح: <b>{fmt_amount(u['balance'])}</b>\n"
        f"🔻 الحد الأدنى للشحن: <b>{fmt_amount(MIN_ICHANCY_DEPOSIT)}</b>\n\n"
        "أدخل المبلغ الذي ترغب بإضافته إلى حسابك."
    ), inline([nav_row("home", "🔙 إلغاء")]))


def show_ichancy_withdraw(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        return set_panel(chat_id, (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        ), inline([[cb("🎮 إنشاء حساب iChancy", "ichancy_create")], nav_row("home")]))
    return set_panel(chat_id, (
        "⚠️ <b>الخدمة غير متاحة مؤقتًا</b>\n\n"
        f"🔻 الحد الأدنى للسحب من iChancy: <b>{fmt_amount(MIN_ICHANCY_WITHDRAW)}</b>\n\n"
        "يرجى المحاولة بعد قليل."
    ), inline([nav_row("home")]))


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
        registration = enqueue_player_registration(chat_id, flow["username"], password)
        if registration.get("ok"):
            set_panel(chat_id, "⏳ <b>جارٍ إنشاء حساب iChancy…</b>", inline([nav_row("home")]))
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
        set_panel(chat_id, "🧪 <b>الواجهة جاهزة للاختبار</b>\n\nلم يتم إرسال أي مبلغ إلى iChancy لأن الجسر لم يتم ربطه بعد.", inline([nav_row("home")]))
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


def bridge_json_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


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
    return {"ok": True, "job_id": str(job["job_id"])}


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
            # Requeue work abandoned by a disconnected bridge after 2 minutes.
            cur.execute("""
                UPDATE bridge_jobs SET status='pending', claimed_by=NULL, updated_at=NOW()
                WHERE status='running' AND updated_at < NOW() - INTERVAL '2 minutes'
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
                    SELECT job_id::text AS job_id, request_id, job_type, payload, attempts
                    FROM bridge_jobs
                    WHERE status='pending' AND job_type = ANY(%s)
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """, (requested_types,))
            else:
                cur.execute("""
                    SELECT job_id::text AS job_id, request_id, job_type, payload, attempts
                    FROM bridge_jobs
                    WHERE status='pending'
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                """)
            row = cur.fetchone()
            if not row:
                return jsonify({"ok": True, "job": None})
            cur.execute("""
                UPDATE bridge_jobs
                SET status='running', attempts=attempts+1, claimed_by=%s,
                    started_at=COALESCE(started_at,NOW()), updated_at=NOW()
                WHERE job_id=%s
            """, (device_id, row["job_id"]))
    return jsonify({"ok": True, "job": dict(row)})


@app.route("/bridge/v1/jobs/<job_id>/complete", methods=["POST"])
def bridge_complete_job(job_id):
    if not bridge_authorized(request):
        return bridge_json_error("unauthorized", 401)
    body = request.get_json(silent=True) or {}
    device_id = str(body.get("device_id", "")).strip()
    status = body.get("status")
    if status not in {"succeeded", "failed"}:
        return bridge_json_error("status must be succeeded or failed")
    if not db_enabled():
        return bridge_json_error("postgres_required", 503)
    ensure_db()
    completed_job = None
    accepted_completion = False
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bridge_jobs
                SET status=%s, result=%s, error=%s, finished_at=NOW(), updated_at=NOW()
                WHERE job_id=%s AND status='running' AND claimed_by=%s
                RETURNING job_id::text AS job_id, request_id, status
            """, (status, Json(body.get("result") or {}), str(body.get("error", ""))[:500] or None, job_id, device_id))
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
                elif telegram_id:
                    cur.execute("""
                        UPDATE users
                        SET ichancy_creation_status='failed', updated_at=NOW()
                        WHERE telegram_id=%s
                    """, (int(telegram_id),))
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
                detail = error.removeprefix("register_player_http_").replace("_", " ")
                message = f"⚠️ <b>رفض iChancy إنشاء الحساب.</b>\n\nالرد: <code>{html.escape(detail)}</code>\n\nلم يتم اعتماد الحساب. تأكد من صلاحية Agent الأب ثم أعد المحاولة."
            else:
                message = "⚠️ <b>لم يكتمل إنشاء حساب iChancy.</b>\n\nلم يتم إنشاء حساب جديد. اختر اسم مستخدم آخر أو أعد المحاولة لاحقًا."
            set_panel(int(chat_id), message, inline([[cb("🔁 اختيار اسم آخر", "ichancy_create")], nav_row("account")]))
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
            print("Callback error:", repr(exc), flush=True)
            traceback.print_exc()
            callback_id = query.get("id")
            if callback_id:
                answer_callback(callback_id, "⚠️ حدث خطأ مؤقت، حاول مرة ثانية.", True)
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            print(f"callback {query.get('data','')} {elapsed_ms}ms", flush=True)
        return jsonify({"ok": True})

    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text", "")
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
        print("Message handler error:", repr(exc), flush=True)
        traceback.print_exc()
        send_message(chat_id, "⚠️ حدث خطأ مؤقت، حاول مرة ثانية.")

    return jsonify({"ok": True})


@app.route("/diagnostics")
def diagnostics():
    wh = tg("getWebhookInfo", timeout=5)
    result = wh.get("result") or {} if isinstance(wh, dict) else {}
    return jsonify({
        "ok": True,
        "version": "v18-sham-syp-qr-request",
        "db": "postgres" if db_enabled() else "memory",
        "sham_qr": os.path.exists(SHAM_SYP_QR_PATH),
        "webhook_pending_updates": result.get("pending_update_count"),
        "last_webhook_error": result.get("last_error_message"),
    })


@app.route("/set-webhook")
def set_webhook():
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
