import os
import re
import html
import json
import base64
import hashlib
import secrets
import traceback
import time
import threading
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

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0") or 0)
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://asmar-robert-bot.onrender.com"
).rstrip("/")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest() if BOT_TOKEN else ""

# Reuse HTTPS connections to Telegram instead of performing a new TLS handshake
# for every button press. This noticeably improves inline-button latency.
TG_SESSION = requests.Session()
TG_ADAPTER = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
TG_SESSION.mount("https://", TG_ADAPTER)

_DB_POOL = None
_DB_POOL_LOCK = threading.Lock()
_BONUS_CACHE = {"expires": 0.0, "value": dict()}
_SUPPORT_CACHE = {"expires": 0.0, "value": []}

# -----------------------------------------------------------------------------
# Persistent storage
# -----------------------------------------------------------------------------
# When DATABASE_URL is configured, important user data lives in PostgreSQL.
# If DATABASE_URL is missing, the bot falls back to temporary in-memory storage
# so the UI can still be tested safely.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# Short-lived interface state can stay in RAM. Losing it on a deploy only means
# the user returns to the main menu; balances/accounts remain in PostgreSQL.
flows = {}
panel_message_ids = {}
password_visible = {}

# UI-only fallback when no database is connected.
users = {}
transactions_mem = []
bonuses_mem = {}
_db_initialized = False


DEFAULT_BONUSES = {
    "sham": 0,
    "syriatel": 0,
    "usdt": 0,
    "wish": 0,
}

# V17 transaction minimums. Funding minimums are in the currency the customer
# actually sends. Bot/iChancy minimums are in bot-balance units.
MIN_TOPUP_SYP = 20_000
MIN_TOPUP_USD = Decimal("2")
MIN_BOT_WITHDRAW = 20_000
MIN_ICHANCY_DEPOSIT = 20_000
MIN_ICHANCY_WITHDRAW = 50_000



def _fernet():
    """Encrypt recoverable credentials at rest.

    Prefer APP_ENCRYPTION_KEY when configured. For easier testing, we can derive
    a stable key from BOT_TOKEN. Rotating BOT_TOKEN without setting a dedicated
    APP_ENCRYPTION_KEY would make old encrypted passwords unreadable, so a
    dedicated key is recommended before production.
    """
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
                1, 8, DATABASE_URL,
                cursor_factory=RealDictCursor,
                connect_timeout=4,
                application_name="asmar-robert-customer-bot",
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )
    return _DB_POOL


@contextmanager
def db_conn():
    """Borrow one PostgreSQL connection and always return it to the pool.

    V11 created a fresh connection on many button presses without explicitly
    closing it. Over time those connections accumulated and could make callbacks
    appear frozen. V12 fixes that leak and reuses a small bounded pool.
    """
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
    global _db_initialized
    if not db_enabled() or _db_initialized:
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
                    referred_by BIGINT,
                    referral_earnings BIGINT NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
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
            # Safe in-place migration for older V9 databases. No existing data is deleted.
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
            for method, percent in DEFAULT_BONUSES.items():
                cur.execute(
                    "INSERT INTO bonuses(method, percent) VALUES(%s, %s) ON CONFLICT(method) DO NOTHING",
                    (method, percent),
                )
    _db_initialized = True


def get_bonuses():
    """Read current bonus percentages with a tiny cache for fast menus."""
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
        if chat_id not in users:
            users[chat_id] = {
                "balance": 0,
                "ichancy_username": None,
                "ichancy_password": None,
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
        if chat_id not in users:
            upsert_user(chat_id)
        u = dict(users[chat_id])
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
        "password_visible": password_visible.get(chat_id, False),
        "referred_by": row["referred_by"],
        "referral_earnings": int(row["referral_earnings"] or 0),
    }


def save_ichancy_credentials(chat_id, username, password):
    """Persist the customer's chosen iChancy credentials."""
    if not db_enabled():
        get_user(chat_id)
        users[chat_id]["ichancy_username"] = username
        users[chat_id]["ichancy_password"] = password
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


def username_taken(username, except_chat_id=None):
    """Case-insensitive duplicate check before asking for password."""
    if not db_enabled():
        for cid, u in users.items():
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
    now = datetime.now(timezone.utc)
    return f"{prefix}-{now.strftime('%y%m%d')}-{secrets.token_hex(4).upper()}"


def add_transaction(chat_id, tx_type, amount=0, status="completed", method=None, details=None,
                    balance_before=None, balance_after=None, conn=None):
    """Write one immutable ledger row and return its public reference code.

    If an existing DB connection is supplied, the insert participates in the same
    atomic transaction as the balance change. This is the pattern future real
    deposit/withdraw handlers should use.
    """
    details = details or {}
    tx_code = make_tx_code()
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
        with connection.cursor() as cur:
            # Extremely unlikely collision, but retry safely if it ever happens.
            code = tx_code
            for _ in range(3):
                try:
                    cur.execute("""
                        INSERT INTO transactions(
                            telegram_id, tx_code, tx_type, amount, status, method, details,
                            balance_before, balance_after
                        )
                        VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        chat_id, code, tx_type, int(amount), status, method, Json(details),
                        balance_before, balance_after
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


def redeem_gift_code(chat_id, raw_code):
    """Redeem one globally single-use gift code atomically.

    The code row is locked while it is checked and consumed, so two users
    cannot successfully redeem the same code at the same time. The balance
    credit and ledger entry commit in the same PostgreSQL transaction.
    """
    code = (raw_code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        return {"ok": False, "reason": "invalid"}

    if not db_enabled():
        return {"ok": False, "reason": "storage_unavailable"}

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code, amount, used_by, used_at FROM gift_codes WHERE code=%s FOR UPDATE",
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
            if amount <= 0:
                return {"ok": False, "reason": "invalid"}

            before = int(user["balance"] or 0)
            after = before + amount
            cur.execute(
                "UPDATE users SET balance=%s, updated_at=NOW() WHERE telegram_id=%s",
                (after, chat_id),
            )
            cur.execute(
                "UPDATE gift_codes SET used_by=%s, used_at=NOW() WHERE code=%s AND used_by IS NULL",
                (chat_id, code),
            )
            if cur.rowcount != 1:
                return {"ok": False, "reason": "used"}

        tx_code = add_transaction(
            chat_id,
            "gift",
            amount,
            "completed",
            "gift-code",
            {"gift_code": code},
            before,
            after,
            conn=conn,
        )

    return {
        "ok": True,
        "code": code,
        "amount": amount,
        "before": before,
        "after": after,
        "tx_code": tx_code,
    }


def set_test_balance_with_ledger(chat_id, amount):
    """Admin-only test helper: balance update + ledger entry in one DB transaction."""
    amount = int(amount)
    if not db_enabled():
        u = get_user(chat_id)
        before = int(u.get("balance", 0))
        users[chat_id]["balance"] = amount
        code = add_transaction(
            chat_id, "test_credit", abs(amount - before), "completed", "admin-test",
            {"note": "UI test balance set"}, before, amount
        )
        return before, amount, code

    ensure_db()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id=%s FOR UPDATE", (chat_id,))
            row = cur.fetchone()
            if not row:
                raise RuntimeError("user missing")
            before = int(row["balance"] or 0)
            cur.execute(
                "UPDATE users SET balance=%s, updated_at=NOW() WHERE telegram_id=%s",
                (amount, chat_id),
            )
        code = add_transaction(
            chat_id, "test_credit", abs(amount - before), "completed", "admin-test",
            {"note": "UI test balance set"}, before, amount, conn=conn
        )
    return before, amount, code


def list_transactions(chat_id, limit=10, offset=0, tx_types=None):
    tx_types = list(tx_types or [])
    if not db_enabled():
        items = [x for x in transactions_mem if x["telegram_id"] == chat_id]
        if tx_types:
            items = [x for x in items if x.get("tx_type") in tx_types]
        items = items[::-1]
        return items[int(offset):int(offset) + int(limit)]
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


def referral_stats(chat_id):
    if not db_enabled():
        count = sum(1 for u in users.values() if u.get("referred_by") == chat_id)
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
    """Enable Telegram's native bottom-left Menu button.

    The native menu contains one ready command only: /start — START.
    """
    commands = [
        {"command": "start", "description": "START"},
    ]
    tg("setMyCommands", {"commands": commands})
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


def fmt_amount(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return "0"


def main_inline_keyboard():
    return inline([
        [cb("🎮 حساب iChancy 🎮", "account")],
        [cb("⬇️ شحن رصيد البوت", "topup"), cb("⬆️ سحب رصيد البوت", "withdraw_bot")],
        [cb("🎮 شحن حساب iChancy", "ichancy_deposit"), cb("💸 سحب من حساب iChancy", "ichancy_withdraw")],
        [cb("📋 سجل العمليات", "history"), cb("🎁 العروض والبونصات", "offers")],
        [cb("👥 نظام الإحالات", "referrals"), cb("🎟️ كود الهدية", "gift")],
        [cb("💬 الدعم والمساعدة", "support"), cb("📜 الشروط والخدمات", "terms")],
    ])


def remove_reply_keyboard():
    return {"remove_keyboard": True}


def inline(rows):
    return {"inline_keyboard": rows}


def cb(text, data):
    return {"text": text, "callback_data": data}


def nav_row(back_data="home", back_label="🔙 رجوع"):
    """Single inline back button. Telegram handles the native START button itself."""
    return [cb(back_label, back_data)]


def url_btn(text, url):
    return {"text": text, "url": url}


def copy_btn(text, value):
    # Telegram Bot API supports copy_text buttons.
    return {"text": text, "copy_text": {"text": str(value)}}


def greeting(chat_id):
    u = get_user(chat_id)
    return (
        "👑 <b>اهــــلا بالمــــــلك</b> 👑\n"
        f"🆔 معرفك على البوت: <code>{chat_id}</code>\n"
        f"💰 رصيدك: <b>{fmt_amount(u['balance'])}</b>\n"
        "👑 <b>نفتخر بانضمامك يامــلك</b> 👑"
    )


def set_panel(chat_id, text, reply_markup=None, force_new=False):
    """Edit the current bot panel when possible; otherwise send a new one."""
    mid = panel_message_ids.get(chat_id)
    if mid and not force_new:
        res = edit_message(chat_id, mid, text, reply_markup)
        if res.get("ok"):
            return res
        desc = str(res.get("description", "")).lower()
        if "message is not modified" in desc:
            return {"ok": True, "result": {"message_id": mid}}

    res = send_message(chat_id, text, reply_markup)
    if res.get("ok") and res.get("result"):
        panel_message_ids[chat_id] = res["result"]["message_id"]
    return res


def show_home(chat_id, force_new=False):
    flows.pop(chat_id, None)
    # /start can force a fresh message below the command. Inline navigation still
    # edits the active panel to keep normal browsing clean and fast.
    return set_panel(
        chat_id,
        greeting(chat_id),
        main_inline_keyboard(),
        force_new=force_new,
    )


def show_account(chat_id, created=False):
    u = get_user(chat_id)
    flows.pop(chat_id, None)
    if not u["ichancy_username"]:
        text = (
            "🎮 <b>حساب iChancy</b> 🎮\n\n"
            "لا يوجد حساب iChancy مرتبط بحسابك حاليًا."
        )
        markup = inline([
            [cb("➕ إنشاء حساب جديد", "ichancy_create")],
            nav_row("home"),
        ])
        return set_panel(chat_id, text, markup)

    pwd = u["ichancy_password"] or ""
    shown = html.escape(pwd) if u.get("password_visible") else "••••••••"
    username = html.escape(u["ichancy_username"])
    title = "✅ <b>تم إنشاء حسابك بنجاح</b>" if created else "🔐 <b>بيانات تسجيل الدخول للحساب</b>"
    text = (
        f"{title}\n\n"
        f"👤 اسم المستخدم: <code>{username}</code>\n"
        f"🔑 كلمة المرور: <code>{shown}</code>"
    )
    toggle_label = "🙈 إخفاء كلمة المرور" if u.get("password_visible") else "👁 عرض كلمة المرور"
    markup = inline([
        [copy_btn("📋 نسخ اسم المستخدم", u["ichancy_username"])],
        [cb(toggle_label, "ichancy_toggle_password")],
        [copy_btn("📋 نسخ كلمة المرور", pwd)],
        [url_btn("🌐 الدخول إلى iChancy", "https://www.ichancy200.com")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def show_topup(chat_id):
    u = get_user(chat_id)
    b = get_bonuses()
    text = (
        "💳 <b>شحن رصيد البوت</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "🎁 <b>البونصات المتاحة حاليًا</b>\n"
        f"💸 Sham Cash: <b>+{b['sham']}%</b>\n"
        f"🔴 سيريتيل كاش: <b>+{b['syriatel']}%</b>\n"
        f"🟢 USDT: <b>+{b['usdt']}%</b>\n"
        f"🟣 Wish Money: <b>+{b['wish']}%</b>\n\n"
        "اختر وسيلة الشحن المناسبة:"
    )
    markup = inline([
        [cb("💸 Sham Cash", "topup_sham"), cb("🔴 Syriatel Cash", "topup_syriatel")],
        [cb("🟢 USDT", "topup_usdt"), cb("🟣 Wish Money", "topup_wish")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def show_sham_cash_currency(chat_id):
    text = (
        "💸 <b>Sham Cash</b>\n\n"
        "اختر العملة التي تريد الإيداع بها:"
    )
    markup = inline([
        [cb("💸 Sham Cash ليرة", "topup_sham_syp")],
        [cb("💲 Sham Cash Dollar", "topup_sham_usd")],
        nav_row("topup"),
    ])
    return set_panel(chat_id, text, markup)


def show_usdt_networks(chat_id):
    text = (
        "🟢 <b>الشحن عبر USDT</b>\n\n"
        "💵 الحد الأدنى للإيداع: <b>$2</b>\n\n"
        "اختر شبكة التحويل:"
    )
    markup = inline([
        [cb("🔴 USDT TRC20", "usdt_trc20")],
        [cb("🟡 USDT BEP20", "usdt_bep20")],
        nav_row("topup"),
    ])
    return set_panel(chat_id, text, markup)


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
    text = (
        f"💳 <b>{html.escape(method_name)}</b>\n\n"
        f"🔻 الحد الأدنى للإيداع: <b>{minimum_text}</b>\n\n"
        f"{prompt}"
    )
    return set_panel(chat_id, text, inline([nav_row(back_data, "🔙 إلغاء")]))


def show_topup_ready(chat_id, method_name, currency, amount):
    # Temporary final screen until the payment API + image/instructions are wired.
    if currency == "SYP":
        amount_text = f"{fmt_amount(int(amount))} ل.س"
    else:
        amount_text = f"${format(amount, 'f').rstrip('0').rstrip('.')}"
    text = (
        "✅ <b>تم قبول المبلغ</b>\n\n"
        f"💳 الطريقة: <b>{html.escape(method_name)}</b>\n"
        f"💰 المبلغ: <b>{amount_text}</b>\n\n"
        "🚧 سيتم إضافة صورة وتعليمات التحويل والتحقق التلقائي لهذه الطريقة لاحقًا."
    )
    return set_panel(chat_id, text, inline([nav_row("topup")]))


def show_withdraw_bot(chat_id):
    u = get_user(chat_id)
    text = (
        "💸 <b>سحب رصيد البوت</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n\n"
        "اختر طريقة السحب المناسبة:"
    )
    markup = inline([
        [cb("🟩 شام كاش", "wd_sham"), cb("🔴 سيريتيل كاش", "wd_syriatel")],
        [cb("🟢 USDT", "wd_usdt"), cb("🟣 Wish Money", "wd_wish")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def show_withdraw_method(chat_id, method_name):
    u = get_user(chat_id)
    flows[chat_id] = {"step": "withdraw_bot_amount", "method": method_name}
    text = (
        f"💸 <b>السحب عبر {html.escape(method_name)}</b>\n\n"
        f"💰 رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
        f"🔻 الحد الأدنى للسحب: <b>{fmt_amount(MIN_BOT_WITHDRAW)}</b>\n\n"
        "أدخل المبلغ المطلوب سحبه من رصيد البوت."
    )
    return set_panel(chat_id, text, inline([nav_row("withdraw_bot", "🔙 إلغاء")]))


def show_ichancy_deposit(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        text = (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        )
        return set_panel(chat_id, text, inline([
            [cb("🎮 إنشاء حساب iChancy", "ichancy_create")],
            nav_row("home"),
        ]))
    flows[chat_id] = {"step": "ichancy_deposit_amount"}
    text = (
        "🎮 <b>شحن حساب iChancy</b>\n\n"
        f"💰 رصيدك المتاح: <b>{fmt_amount(u['balance'])}</b>\n"
        f"🔻 الحد الأدنى للشحن: <b>{fmt_amount(MIN_ICHANCY_DEPOSIT)}</b>\n\n"
        "أدخل المبلغ الذي ترغب بإضافته إلى حسابك."
    )
    return set_panel(chat_id, text, inline([nav_row("home", "🔙 إلغاء")]))


def show_ichancy_withdraw(chat_id):
    u = get_user(chat_id)
    if not u["ichancy_username"]:
        text = (
            "⚠️ <b>لا يوجد حساب iChancy مرتبط بحسابك.</b>\n\n"
            "أنشئ حساب iChancy أولًا للمتابعة."
        )
        return set_panel(chat_id, text, inline([
            [cb("🎮 إنشاء حساب iChancy", "ichancy_create")],
            nav_row("home"),
        ]))
    # Bridge is not connected in this UI prototype yet.
    text = (
        "⚠️ <b>الخدمة غير متاحة مؤقتًا</b>\n\n"
        f"🔻 الحد الأدنى للسحب من iChancy: <b>{fmt_amount(MIN_ICHANCY_WITHDRAW)}</b>\n\n"
        "يرجى المحاولة بعد قليل."
    )
    return set_panel(chat_id, text, inline([nav_row("home")]))


def show_history(chat_id):
    text = "📋 <b>سجل العمليات</b>\n\nاختر نوع العمليات:"
    markup = inline([
        [cb("⬇️ شحن رصيد البوت", "history:bot_topup:0")],
        [cb("⬆️ سحب رصيد البوت", "history:bot_withdraw:0")],
        [cb("🎮 شحن iChancy", "history:ichancy_deposit:0")],
        [cb("↩️ سحب من iChancy", "history:ichancy_withdraw:0")],
        [cb("📋 جميع العمليات", "history:all:0")],
        nav_row("home"),
    ])
    return set_panel(chat_id, text, markup)


def _history_types(filter_name):
    mapping = {
        "bot_topup": ["bot_topup"],
        "bot_withdraw": ["bot_withdraw"],
        "ichancy_deposit": ["ichancy_deposit"],
        "ichancy_withdraw": ["ichancy_withdraw"],
    }
    return mapping.get(filter_name, [])


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
    if tx_type == "test_credit":
        before = item.get("balance_before")
        after = item.get("balance_after")
        if before is not None and after is not None:
            return "+" if int(after) >= int(before) else "-"
    return ""


def show_history_page(chat_id, filter_name="all", page=0):
    page = max(0, int(page))
    per_page = 10
    types = _history_types(filter_name)
    # Fetch one extra row only to know whether a Next button is needed.
    items = list_transactions(chat_id, per_page + 1, page * per_page, types)
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
    status_labels = {
        "completed": "✅ مكتملة",
        "pending": "⏳ قيد المعالجة",
        "approved": "🟡 مقبولة",
        "rejected": "❌ مرفوضة",
        "refunded": "↩️ تم إرجاع الرصيد",
        "failed": "❌ فشلت",
    }

    lines = [f"{_history_title(filter_name)}", ""]
    if not items:
        lines.append("لا توجد عمليات مسجلة.")
    else:
        for idx, item in enumerate(items):
            lines.append(labels.get(item.get("tx_type"), "🧾 عملية"))
            prefix = _amount_prefix(item)
            lines.append(f"💰 <b>{prefix}{fmt_amount(item.get('amount', 0))}</b>")
            if item.get("method"):
                lines.append(f"💳 {html.escape(str(item['method']))}")
            lines.append(status_labels.get(item.get("status"), html.escape(str(item.get("status", "")))))
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
    text = (
        "🎁 <b>العروض والبونصات الحالية</b>\n\n"
        f"💸 Sham Cash: <b>+{b['sham']}%</b>\n"
        f"🔴 سيريتيل كاش: <b>+{b['syriatel']}%</b>\n"
        f"🟢 USDT: <b>+{b['usdt']}%</b>\n"
        f"🟣 Wish Money: <b>+{b['wish']}%</b>\n\n"
        "استفد من البونص عند شحن رصيد البوت عبر الوسيلة التي عليها عرض."
    )
    return set_panel(chat_id, text, inline([
        [cb("⬇️ شحن رصيد البوت", "topup")],
        nav_row("home"),
    ]))


def show_referrals(chat_id, bot_username=None):
    bot_username = bot_username or os.environ.get("BOT_USERNAME", "YourBot")
    link = f"https://t.me/{bot_username}?start=ref_{chat_id}"
    ref_count, ref_earnings = referral_stats(chat_id)
    text = (
        "👥 <b>نظام الإحالات</b>\n\n"
        "شارك رابطك الخاص وادعُ أصدقاءك للانضمام إلى البوت.\n\n"
        f"👤 عدد الأشخاص المسجلين عن طريقك: <b>{ref_count}</b>\n"
        f"💰 أرباح الإحالات: <b>{fmt_amount(ref_earnings)}</b>\n\n"
        f"🔗 رابط الإحالة الخاص بك:\n<code>{html.escape(link)}</code>"
    )
    return set_panel(chat_id, text, inline([
        [copy_btn("📋 نسخ رابط الإحالة", link)],
        nav_row("home"),
    ]))


def show_gift(chat_id):
    flows[chat_id] = {"step": "gift_code"}
    text = (
        "🎟️ <b>كود الهدية</b>\n\n"
        "أدخل كود الهدية للاستفادة من المكافأة.\n\n"
        "كل كود صالح لاستخدام واحد فقط على مستوى البوت بالكامل."
    )
    return set_panel(chat_id, text, inline([nav_row("home", "🔙 إلغاء")]))


def support_usernames():
    result = db_support_usernames()
    if result:
        return result
    raw = os.environ.get("SUPPORT_USERNAMES", "")
    result = []
    for x in raw.split(","):
        x = x.strip().lstrip("@")
        if x and re.fullmatch(r"[A-Za-z0-9_]{5,32}", x):
            result.append(x)
    return result


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
    text = (
        "📜 <b>الشروط والخدمات</b>\n\n"
        "يُرجى قراءة الشروط بعناية قبل استخدام الخدمات.\n\n"
        "• تأكد من صحة بيانات حساب iChancy قبل تنفيذ أي عملية.\n"
        "• طلبات سحب رصيد البوت تخضع لمراجعة الإدارة قبل التنفيذ.\n"
        "• بعد تقديم طلب السحب، تتم معالجة الحوالة بأسرع وقت ممكن، وعادةً خلال ساعة إلى 3 ساعات.\n"
        "• يجب إدخال بيانات وسيلة الاستلام بشكل صحيح.\n"
        "• يحق للإدارة مراجعة أي عملية مشبوهة لحماية الحسابات والأرصدة.\n"
        "• نسب البونص والعروض قابلة للتغيير وتظهر داخل قسم شحن رصيد البوت.\n\n"
        "👑 شكرًا لثقتكم بخدماتنا"
    )
    return set_panel(chat_id, text, inline([nav_row("home")]))


def ichancy_create_rejection_message(reason):
    """Map future iChancy/bridge create-account rejections to customer-safe text.

    The current UI prototype does not call the cashier yet. Once the Android
    bridge is connected, username-conflict responses should route back to the
    username step using this message.
    """
    normalized = str(reason or "").strip().lower()
    username_conflicts = {
        "username_exists", "username_taken", "duplicate_username",
        "user_exists", "login_exists", "already_exists",
    }
    if normalized in username_conflicts:
        return "❌ اسم المستخدم مستخدم من قبل، اختر اسمًا آخر."
    return "⚠️ تعذر إنشاء الحساب حاليًا، حاول مرة ثانية."


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
        return False

    step = flow.get("step")
    u = get_user(chat_id)

    if step == "ichancy_username":
        candidate = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,24}", candidate):
            set_panel(
                chat_id,
                "👤 <b>اكتب اسم المستخدم</b>\n\n"
                "اسم المستخدم غير صالح.",
                inline([nav_row("account", "🔙 إلغاء")])
            )
            return True
        if username_taken(candidate, chat_id):
            set_panel(
                chat_id,
                "👤 <b>اكتب اسم المستخدم</b>\n\n❌ اسم المستخدم مستخدم من قبل، اختر اسمًا آخر.",
                inline([nav_row("account", "🔙 إلغاء")])
            )
            return True
        flow["username"] = candidate
        flow["step"] = "ichancy_password"
        set_panel(
            chat_id,
            "🔐 <b>اكتب كلمة المرور</b>",
            inline([nav_row("account", "🔙 إلغاء")])
        )
        return True

    if step == "ichancy_password":
        password = text.strip()
        if len(password) < 6 or len(password) > 64:
            set_panel(
                chat_id,
                "🔐 <b>اكتب كلمة المرور</b>\n\n"
                "كلمة المرور قصيرة جدًا.",
                inline([nav_row("account", "🔙 إلغاء")])
            )
            return True
        # UI prototype only: save in our persistent DB. No real iChancy request is sent yet.
        ok, reason = save_ichancy_credentials(chat_id, flow["username"], password)
        if not ok:
            flow["step"] = "ichancy_username"
            set_panel(
                chat_id,
                "👤 <b>اكتب اسم المستخدم</b>\n\n❌ اسم المستخدم مستخدم من قبل، اختر اسمًا آخر.",
                inline([nav_row("account", "🔙 إلغاء")])
            )
            return True
        password_visible[chat_id] = False
        flows.pop(chat_id, None)
        show_account(chat_id, created=True)
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
                set_panel(
                    chat_id,
                    f"❌ <b>الحد الأدنى للإيداع هو {fmt_amount(minimum)} ل.س</b>\n\nأدخل مبلغًا أعلى.",
                    inline([nav_row(back_data, "🔙 إلغاء")])
                )
                return True
            flows.pop(chat_id, None)
            show_topup_ready(chat_id, method, currency, Decimal(amount))
            return True

        amount = parse_decimal_amount(text)
        minimum = Decimal(flow.get("minimum", str(MIN_TOPUP_USD)))
        if amount is None:
            set_panel(
                chat_id,
                "⚠️ أدخل مبلغًا صحيحًا، مثال: <code>2</code> أو <code>2.5</code>.",
                inline([nav_row(back_data, "🔙 إلغاء")])
            )
            return True
        if amount < minimum:
            set_panel(
                chat_id,
                f"❌ <b>الحد الأدنى للإيداع هو ${minimum}</b>\n\nأدخل مبلغًا أعلى.",
                inline([nav_row(back_data, "🔙 إلغاء")])
            )
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
            set_panel(
                chat_id,
                f"❌ <b>الحد الأدنى للسحب هو {fmt_amount(MIN_BOT_WITHDRAW)}</b>\n\nأدخل مبلغًا أعلى.",
                inline([nav_row("withdraw_bot", "🔙 إلغاء")])
            )
            return True
        if amount > u["balance"]:
            set_panel(
                chat_id,
                "⚠️ <b>الرصيد غير كافٍ</b>\n\n"
                f"رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
                f"المبلغ المطلوب: <b>{fmt_amount(amount)}</b>",
                inline([[cb("🔙 رجوع", "withdraw_bot")]])
            )
            flows.pop(chat_id, None)
            return True
        method = flow.get("method", "غير محدد")
        flows.pop(chat_id, None)
        # No real balance mutation in the UI-only prototype.
        text2 = (
            "✅ <b>تم استلام طلب السحب</b>\n\n"
            f"💰 المبلغ: <b>{fmt_amount(amount)}</b>\n"
            f"💳 طريقة الاستلام: <b>{html.escape(method)}</b>\n\n"
            "⏱️ سيتم تنفيذ طلبك بأسرع وقت ممكن، وعادةً خلال مدة تتراوح بين ساعة و3 ساعات.\n"
            "سيتم إشعارك فور اكتمال الحوالة. 👑\n\n"
            "🧪 <i>طلب تجريبي فقط، لم يتم خصم أو تحويل أي رصيد.</i>"
        )
        set_panel(chat_id, text2, inline([nav_row("home")]))
        return True

    if step == "ichancy_deposit_amount":
        amount = parse_amount(text)
        if amount is None:
            set_panel(chat_id, "⚠️ أدخل مبلغًا صحيحًا بالأرقام فقط.", inline([nav_row("home", "🔙 إلغاء")]))
            return True
        if amount < MIN_ICHANCY_DEPOSIT:
            set_panel(
                chat_id,
                f"❌ <b>الحد الأدنى لشحن iChancy هو {fmt_amount(MIN_ICHANCY_DEPOSIT)}</b>\n\nأدخل مبلغًا أعلى.",
                inline([nav_row("home", "🔙 إلغاء")])
            )
            return True
        flows.pop(chat_id, None)
        if amount > u["balance"]:
            set_panel(
                chat_id,
                "⚠️ <b>الرصيد غير كافٍ</b>\n\n"
                f"رصيدك الحالي: <b>{fmt_amount(u['balance'])}</b>\n"
                f"المبلغ المطلوب: <b>{fmt_amount(amount)}</b>",
                inline([nav_row("home")])
            )
            return True
        set_panel(
            chat_id,
            "🧪 <b>الواجهة جاهزة للاختبار</b>\n\n"
            "لم يتم إرسال أي مبلغ إلى iChancy لأن الجسر لم يتم ربطه بعد.",
            inline([nav_row("home")])
        )
        return True

    if step == "gift_code":
        flows.pop(chat_id, None)
        result = redeem_gift_code(chat_id, text)
        if result.get("ok"):
            set_panel(
                chat_id,
                "🎉 <b>تم استخدام كود الهدية بنجاح</b>\n\n"
                f"💰 تمت إضافة: <b>{fmt_amount(result['amount'])}</b>\n"
                f"💳 رصيدك الحالي: <b>{fmt_amount(result['after'])}</b>\n"
                f"🧾 <code>{html.escape(result['tx_code'])}</code>",
                inline([nav_row("home")])
            )
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


def handle_callback(query):
    callback_id = query.get("id")
    data = query.get("data", "")
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return
    if message.get("message_id"):
        panel_message_ids[chat_id] = message["message_id"]

    # Stop Telegram's loading spinner immediately, then render the requested panel.
    answer_callback(callback_id)

    if data == "home":
        flows.pop(chat_id, None)
        set_panel(chat_id, greeting(chat_id), main_inline_keyboard())
    elif data == "account":
        show_account(chat_id)
    elif data == "ichancy_create":
        flows[chat_id] = {"step": "ichancy_username"}
        set_panel(
            chat_id,
            "👤 <b>اكتب اسم المستخدم</b>",
            inline([nav_row("account", "🔙 إلغاء")])
        )
    elif data == "ichancy_toggle_password":
        password_visible[chat_id] = not password_visible.get(chat_id, False)
        show_account(chat_id)
    elif data == "topup":
        show_topup(chat_id)
    elif data == "topup_usdt":
        show_usdt_networks(chat_id)
    elif data == "topup_sham":
        show_sham_cash_currency(chat_id)
    elif data == "topup_sham_syp":
        show_topup_amount_prompt(chat_id, "Sham Cash ليرة", "SYP", MIN_TOPUP_SYP, "topup_sham")
    elif data == "topup_sham_usd":
        show_topup_amount_prompt(chat_id, "Sham Cash Dollar", "USD", MIN_TOPUP_USD, "topup_sham")
    elif data == "topup_syriatel":
        show_topup_amount_prompt(chat_id, "Syriatel Cash", "SYP", MIN_TOPUP_SYP, "topup")
    elif data == "topup_wish":
        show_topup_amount_prompt(chat_id, "Wish Money", "USD", MIN_TOPUP_USD, "topup")
    elif data == "usdt_trc20":
        show_topup_amount_prompt(chat_id, "USDT - TRC20", "USD", MIN_TOPUP_USD, "topup_usdt")
    elif data == "usdt_bep20":
        show_topup_amount_prompt(chat_id, "USDT - BEP20", "USD", MIN_TOPUP_USD, "topup_usdt")
    elif data == "withdraw_bot":
        flows.pop(chat_id, None)
        show_withdraw_bot(chat_id)
    elif data == "wd_sham":
        show_withdraw_method(chat_id, "شام كاش")
    elif data == "wd_syriatel":
        show_withdraw_method(chat_id, "سيريتيل كاش")
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


@app.before_request
def prepare_storage():
    if db_enabled():
        ensure_db()


@app.route("/")
def home():
    return "Asmar Robert Bot UI + PostgreSQL ledger v16 is running ✅"


@app.route("/health")
def health():
    return jsonify({"ok": True, "mode": "ui-prototype", "storage": "postgres" if db_enabled() else "memory", "ledger": "v17-sham-cash-menu"})


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
        # Always send a NEW home message under /start instead of editing an old
        # menu above in the conversation.
        show_home(chat_id, force_new=True)
        return jsonify({"ok": True})

    if text == "/myid":
        send_message(chat_id, f"🆔 معرفك على البوت: <code>{chat_id}</code>")
        return jsonify({"ok": True})

    # Admin-only temporary UI testing helper. It DOES NOT move real money.
    # Example: /testcredit 200000
    if text.startswith("/testcredit") and chat_id == ADMIN_ID:
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            amount = parse_amount(parts[1])
            if amount is not None:
                before, after, code = set_test_balance_with_ledger(chat_id, amount)
                send_message(
                    chat_id,
                    "🧪 تم ضبط الرصيد التجريبي\n"
                    f"💰 الرصيد: <b>{fmt_amount(after)}</b>\n"
                    f"🧾 <code>{code}</code>"
                )
                return jsonify({"ok": True})
        send_message(chat_id, "الاستخدام: <code>/testcredit 200000</code>")
        return jsonify({"ok": True})

    # Process first, then remove the customer's input. This prevents a message
    # from disappearing into silence if a handler ever throws an exception.
    try:
        if process_text_input(chat_id, text):
            if message_id:
                delete_message(chat_id, message_id)
            return jsonify({"ok": True})

        if handle_menu_text(chat_id, text):
            if message_id:
                delete_message(chat_id, message_id)
            return jsonify({"ok": True})

        # Unknown text: keep user inside the designed interface.
        set_panel(
            chat_id,
            "👑 اختر الخدمة المطلوبة من القائمة.",
            main_inline_keyboard()
        )
        if message_id:
            delete_message(chat_id, message_id)
    except Exception as exc:
        print("Message handler error:", repr(exc), flush=True)
        traceback.print_exc()
        send_message(chat_id, "⚠️ حدث خطأ مؤقت، حاول مرة ثانية.")
    return jsonify({"ok": True})


@app.route("/diagnostics")
def diagnostics():
    """Safe operational status: no tokens, passwords, or DB URLs are exposed."""
    wh = tg("getWebhookInfo", timeout=5)
    result = wh.get("result") or {} if isinstance(wh, dict) else {}
    return jsonify({
        "ok": True,
        "version": "v14-ui-speed",
        "db": "postgres" if db_enabled() else "memory",
        "webhook_pending_updates": result.get("pending_update_count"),
        "last_webhook_error": result.get("last_error_message"),
    })


@app.route("/set-webhook")
def set_webhook():
    ensure_native_menu()
    webhook_url = f"{PUBLIC_BASE_URL}/webhook"
    response = tg("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })
    return jsonify(response)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
