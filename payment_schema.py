"""
Database schema and local transaction helpers for configurable cash payments.

This module is deliberately independent from Telegram, iChancy, and Render.
It adds tables/columns without replacing the existing financial paths.
"""

import hashlib
from decimal import Decimal
from psycopg2.extras import Json

PAYMENT_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS payment_methods (
        id BIGSERIAL PRIMARY KEY,
        method_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        archived_at TIMESTAMPTZ,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (length(trim(method_key)) BETWEEN 2 AND 80),
        CHECK (length(trim(display_name)) BETWEEN 1 AND 160)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_method_directions (
        id BIGSERIAL PRIMARY KEY,
        payment_method_id BIGINT NOT NULL REFERENCES payment_methods(id) ON DELETE RESTRICT,
        direction TEXT NOT NULL CHECK (direction IN ('deposit','withdraw')),
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        payment_currency TEXT NOT NULL,
        reference_currency TEXT NOT NULL,
        network TEXT,
        account_name TEXT,
        account_identifier TEXT,
        instructions TEXT,
        exchange_rate NUMERIC(30,12) NOT NULL,
        rate_definition TEXT NOT NULL,
        conversion_multiplier NUMERIC(30,12) NOT NULL DEFAULT 1,
        fee_type TEXT NOT NULL DEFAULT 'none' CHECK (fee_type IN ('none','fixed','percent')),
        fee_value NUMERIC(30,12) NOT NULL DEFAULT 0,
        bonus_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        bonus_percent NUMERIC(12,6) NOT NULL DEFAULT 0,
        minimum_amount NUMERIC(30,12) NOT NULL,
        maximum_amount NUMERIC(30,12),
        amount_scale SMALLINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (payment_method_id, direction),
        CHECK (exchange_rate > 0),
        CHECK (conversion_multiplier > 0),
        CHECK (fee_value >= 0),
        CHECK (bonus_percent >= 0 AND bonus_percent <= 1000),
        CHECK (minimum_amount >= 0),
        CHECK (maximum_amount IS NULL OR maximum_amount >= minimum_amount),
        CHECK (amount_scale BETWEEN 0 AND 18),
        CHECK (fee_type <> 'percent' OR fee_value <= 100),
        CHECK (length(trim(rate_definition)) >= 8)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_method_fields (
        id BIGSERIAL PRIMARY KEY,
        payment_method_direction_id BIGINT NOT NULL REFERENCES payment_method_directions(id) ON DELETE CASCADE,
        field_key TEXT NOT NULL,
        required_mode TEXT NOT NULL CHECK (required_mode IN ('required','optional','disabled')),
        sort_order INTEGER NOT NULL DEFAULT 0,
        validation_config JSONB NOT NULL DEFAULT '{}'::jsonb,
        UNIQUE (payment_method_direction_id, field_key),
        CHECK (field_key IN ('amount','operation_reference','proof_image','note','recipient_account','recipient_name','network'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_assets (
        id BIGSERIAL PRIMARY KEY,
        payment_method_id BIGINT REFERENCES payment_methods(id) ON DELETE RESTRICT,
        storage_key TEXT NOT NULL UNIQUE,
        content_type TEXT NOT NULL,
        file_size BIGINT NOT NULL,
        sha256 TEXT NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        retention_status TEXT NOT NULL DEFAULT 'retained'
            CHECK (retention_status IN ('retained','eligible','deleted')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        deleted_at TIMESTAMPTZ,
        CHECK (file_size > 0),
        CHECK (content_type IN ('image/jpeg','image/png','image/webp')),
        CHECK (sha256 ~ '^[0-9a-fA-F]{64}$')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cash_request_method_snapshots (
        id BIGSERIAL PRIMARY KEY,
        cash_request_id BIGINT NOT NULL UNIQUE REFERENCES cash_requests(id) ON DELETE RESTRICT,
        payment_method_id BIGINT REFERENCES payment_methods(id) ON DELETE RESTRICT,
        payment_method_direction_id BIGINT REFERENCES payment_method_directions(id) ON DELETE RESTRICT,
        asset_id BIGINT REFERENCES payment_assets(id) ON DELETE RESTRICT,
        snapshot JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cash_request_attachments (
        id BIGSERIAL PRIMARY KEY,
        cash_request_id BIGINT NOT NULL REFERENCES cash_requests(id) ON DELETE RESTRICT,
        storage_key TEXT NOT NULL UNIQUE,
        content_type TEXT NOT NULL,
        file_size BIGINT NOT NULL,
        sha256 TEXT NOT NULL,
        uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        retention_until TIMESTAMPTZ,
        deletion_status TEXT NOT NULL DEFAULT 'retained'
            CHECK (deletion_status IN ('retained','eligible','deleted','blocked_pending_review')),
        CHECK (file_size > 0),
        CHECK (content_type IN ('image/jpeg','image/png','image/webp')),
        CHECK (sha256 ~ '^[0-9a-fA-F]{64}$')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balance_reservations (
        id BIGSERIAL PRIMARY KEY,
        cash_request_id BIGINT NOT NULL UNIQUE REFERENCES cash_requests(id) ON DELETE RESTRICT,
        telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE RESTRICT,
        amount_bot BIGINT NOT NULL CHECK (amount_bot > 0),
        status TEXT NOT NULL DEFAULT 'held'
            CHECK (status IN ('held','released','settled','unknown_external_result')),
        external_outcome TEXT NOT NULL DEFAULT 'unknown_external_result'
            CHECK (external_outcome IN ('external_confirmed_success','external_confirmed_failure','unknown_external_result')),
        external_reference TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_method_audit (
        id BIGSERIAL PRIMARY KEY,
        admin_id BIGINT NOT NULL,
        payment_method_id BIGINT REFERENCES payment_methods(id) ON DELETE RESTRICT,
        direction TEXT CHECK (direction IS NULL OR direction IN ('deposit','withdraw')),
        action TEXT NOT NULL,
        old_value JSONB NOT NULL DEFAULT '{}'::jsonb,
        new_value JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_reconciliations (
        id BIGSERIAL PRIMARY KEY,
        cash_request_id BIGINT REFERENCES cash_requests(id) ON DELETE RESTRICT,
        reservation_id BIGINT REFERENCES balance_reservations(id) ON DELETE RESTRICT,
        reconciliation_key TEXT,
        external_status TEXT NOT NULL,
        local_status TEXT NOT NULL,
        reason TEXT NOT NULL,
        resolution TEXT,
        resolved_by BIGINT,
        resolved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_idempotency (
        operation_type TEXT NOT NULL,
        cash_request_id BIGINT NOT NULL REFERENCES cash_requests(id) ON DELETE RESTRICT,
        idempotency_key TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        response_status INTEGER,
        response_body JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (operation_type, cash_request_id, idempotency_key),
        CHECK (length(trim(idempotency_key)) BETWEEN 8 AND 200),
        CHECK (payload_hash ~ '^[0-9a-fA-F]{64}$')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_audit_log (
        id BIGSERIAL PRIMARY KEY,
        actor_id TEXT NOT NULL,
        actor_type TEXT NOT NULL,
        action TEXT NOT NULL,
        cash_request_id BIGINT REFERENCES cash_requests(id) ON DELETE RESTRICT,
        request_type TEXT,
        previous_status TEXT,
        new_status TEXT,
        decision TEXT,
        reason TEXT NOT NULL,
        operation_ref TEXT,
        external_reference TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cash_request_external_verifications (
        id BIGSERIAL PRIMARY KEY,
        cash_request_id BIGINT NOT NULL REFERENCES cash_requests(id) ON DELETE RESTRICT,
        provider_name TEXT NOT NULL,
        verification_status TEXT NOT NULL
            CHECK (verification_status IN ('verified','failed','unknown')),
        external_reference TEXT,
        amount_original NUMERIC(30,12) NOT NULL,
        currency TEXT NOT NULL,
        beneficiary TEXT NOT NULL,
        provider_trace_id TEXT,
        verified_at TIMESTAMPTZ,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (cash_request_id, provider_name),
        CHECK (amount_original > 0),
        CHECK (verification_status <> 'verified' OR (external_reference IS NOT NULL AND verified_at IS NOT NULL))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_payment_methods_active ON payment_methods(enabled, archived_at, sort_order)",
    "CREATE INDEX IF NOT EXISTS idx_payment_directions_enabled ON payment_method_directions(payment_method_id, enabled, direction)",
    "CREATE INDEX IF NOT EXISTS idx_cash_attachments_request ON cash_request_attachments(cash_request_id, uploaded_at)",
    "CREATE INDEX IF NOT EXISTS idx_reconciliations_open ON financial_reconciliations(cash_request_id) WHERE resolved_at IS NULL",
    "ALTER TABLE financial_reconciliations ADD COLUMN IF NOT EXISTS reconciliation_key TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_reconciliation_once ON financial_reconciliations(cash_request_id, reconciliation_key) WHERE reconciliation_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_financial_audit_request ON financial_audit_log(cash_request_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_external_verification_request ON cash_request_external_verifications(cash_request_id, created_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_external_verification_reference_once ON cash_request_external_verifications(provider_name, external_reference) WHERE external_reference IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_financial_idempotency_request ON financial_idempotency(cash_request_id, created_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_financial_job_once ON transactions(tx_type, (details->>'job_id')) WHERE tx_type IN ('ichancy_deposit','ichancy_withdraw') AND details ? 'job_id'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_financial_request_once ON transactions(tx_type, (details->>'request_id')) WHERE tx_type IN ('ichancy_deposit','ichancy_withdraw') AND details ? 'request_id'",
)


def install_payment_schema(cur):
    """Install additive schema and compatibility columns in one caller transaction."""
    for statement in PAYMENT_SCHEMA_SQL:
        cur.execute(statement)
    # Replace the legacy method-only index with the scoped provider-reference index.
    cur.execute("DROP INDEX IF EXISTS idx_cash_requests_topup_method_ref")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reserved_balance BIGINT NOT NULL DEFAULT 0")
    cur.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname='users_reserved_balance_nonnegative'
            ) THEN
                ALTER TABLE users ADD CONSTRAINT users_reserved_balance_nonnegative
                    CHECK (reserved_balance >= 0);
            END IF;
        END $$
        """
    )
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS payment_method_id BIGINT")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS payment_direction TEXT")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS method_snapshot_id BIGINT")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS reservation_id BIGINT")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS amount_original NUMERIC(30,12)")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS conversion_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS method TEXT")
    cur.execute("ALTER TABLE cash_requests ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb")
    # Provider operation references are unique per payment method and direction.
    # The normalized reference is persisted in details.operation_ref by the flow.
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_requests_topup_provider_ref
        ON cash_requests (
            COALESCE(payment_method_id::text, method),
            COALESCE(payment_direction, 'deposit'),
            lower(btrim(details->>'operation_ref'))
        )
        WHERE request_type='topup'
          AND NULLIF(btrim(details->>'operation_ref'), '') IS NOT NULL
    """)
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS reservation_id BIGINT")
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS cash_request_id BIGINT")
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS settlement_source TEXT")
    cur.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS external_outcome TEXT")
    cur.execute("""
        ALTER TABLE payment_assets
        ADD COLUMN IF NOT EXISTS retention_status TEXT NOT NULL DEFAULT 'retained'
    """)
    cur.execute("""
        ALTER TABLE balance_reservations
        ADD COLUMN IF NOT EXISTS external_outcome TEXT NOT NULL DEFAULT 'unknown_external_result'
    """)
    cur.execute("""
        ALTER TABLE cash_requests
        ADD COLUMN IF NOT EXISTS external_outcome TEXT NOT NULL DEFAULT 'unknown_external_result'
    """)
    cur.execute("""
        CREATE OR REPLACE FUNCTION enforce_withdrawal_reservation_invariant()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            reservation_row RECORD;
        BEGIN
            IF NEW.request_type = 'withdraw'
               AND NEW.status IN ('funds_reserved','approved','payout_in_progress','unknown_external_result','completed') THEN
                IF NEW.reservation_id IS NULL THEN
                    RAISE EXCEPTION 'withdrawal_active_without_reservation:%', NEW.id
                        USING ERRCODE = '23514';
                END IF;
                SELECT id, cash_request_id, telegram_id
                  INTO reservation_row
                  FROM balance_reservations
                 WHERE id = NEW.reservation_id;
                IF NOT FOUND OR reservation_row.cash_request_id <> NEW.id
                   OR reservation_row.telegram_id <> NEW.telegram_id THEN
                    RAISE EXCEPTION 'withdrawal_reservation_mismatch:%', NEW.id
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END $$
    """)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgname='cash_requests_withdrawal_reservation_invariant'
                   AND tgrelid='cash_requests'::regclass
            ) THEN
                CREATE CONSTRAINT TRIGGER cash_requests_withdrawal_reservation_invariant
                AFTER INSERT OR UPDATE OF request_type,status,reservation_id,telegram_id ON cash_requests
                DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION enforce_withdrawal_reservation_invariant();
            END IF;
        END $$
    """)
    for table, name, definition in (
        ("cash_requests", "cash_requests_payment_method_fk", "FOREIGN KEY (payment_method_id) REFERENCES payment_methods(id) ON DELETE RESTRICT"),
        ("cash_requests", "cash_requests_snapshot_fk", "FOREIGN KEY (method_snapshot_id) REFERENCES cash_request_method_snapshots(id) ON DELETE RESTRICT"),
        ("cash_requests", "cash_requests_reservation_fk", "FOREIGN KEY (reservation_id) REFERENCES balance_reservations(id) ON DELETE RESTRICT"),
        ("transactions", "transactions_reservation_fk", "FOREIGN KEY (reservation_id) REFERENCES balance_reservations(id) ON DELETE RESTRICT"),
        ("transactions", "transactions_cash_request_fk", "FOREIGN KEY (cash_request_id) REFERENCES cash_requests(id) ON DELETE RESTRICT"),
    ):
        cur.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname='{name}'
                ) THEN
                    ALTER TABLE {table} ADD CONSTRAINT {name} {definition};
                END IF;
            END $$
            """
        )
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_cash_request_once
        ON transactions(cash_request_id, tx_type)
        WHERE cash_request_id IS NOT NULL AND tx_type IN ('bot_topup','ichancy_deposit','ichancy_withdraw')
    """)


def record_payment_method_audit(cur, admin_id, method_id, direction, action, old_value, new_value):
    cur.execute(
        """
        INSERT INTO payment_method_audit
            (admin_id, payment_method_id, direction, action, old_value, new_value)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (int(admin_id), method_id, direction, action, old_value or {}, new_value or {}),
    )


def record_financial_audit(cur, actor_id, actor_type, action, cash_request_id,
                           request_type, previous_status, new_status, decision,
                           reason, operation_ref=None, external_reference=None,
                           metadata=None):
    cur.execute(
        """
        INSERT INTO financial_audit_log
            (actor_id,actor_type,action,cash_request_id,request_type,previous_status,
             new_status,decision,reason,operation_ref,external_reference,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (str(actor_id), str(actor_type), str(action), cash_request_id, request_type,
         previous_status, new_status, decision, str(reason)[:1000], operation_ref,
         external_reference, Json(metadata or {})),
    )
    return int(cur.fetchone()["id"])


def record_external_verification(cur, cash_request_id, provider_name,
                                 verification_status, external_reference,
                                 amount_original, currency, beneficiary,
                                 provider_trace_id=None, metadata=None):
    """Persist one provider result; never treat admin evidence as provider proof."""
    if verification_status not in {"verified", "failed", "unknown"}:
        raise ValueError("invalid_verification_status")
    if verification_status == "verified" and not external_reference:
        raise ValueError("verified_external_reference_required")
    cur.execute(
        """
        INSERT INTO cash_request_external_verifications
            (cash_request_id,provider_name,verification_status,external_reference,
             amount_original,currency,beneficiary,provider_trace_id,verified_at,metadata)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                CASE WHEN %s='verified' THEN NOW() ELSE NULL END,%s)
        ON CONFLICT (cash_request_id,provider_name) DO NOTHING
        RETURNING id
        """,
        (cash_request_id, str(provider_name), verification_status, external_reference,
         amount_original, str(currency), str(beneficiary), provider_trace_id,
         verification_status, Json(metadata or {})),
    )
    row = cur.fetchone()
    if row:
        return True
    cur.execute(
        """
        SELECT verification_status,external_reference,amount_original,currency,beneficiary
        FROM cash_request_external_verifications
        WHERE cash_request_id=%s AND provider_name=%s
        FOR UPDATE
        """, (cash_request_id, str(provider_name)))
    previous = cur.fetchone()
    if not previous:
        raise RuntimeError("verification_insert_missing")
    same = (
        previous["verification_status"] == verification_status
        and previous["external_reference"] == external_reference
        and str(previous["amount_original"]) == str(amount_original)
        and previous["currency"] == str(currency)
        and previous["beneficiary"] == str(beneficiary)
    )
    if not same:
        raise ValueError("external_verification_conflict")
    return False


def settle_topup_after_verification(cur, cash_request_id, provider_name,
                                    external_reference, amount_original,
                                    currency, beneficiary, actor_id,
                                    actor_type="verification_provider"):
    """Credit one top-up only after a persisted, matching verified result."""
    cur.execute(
        """
        SELECT id,telegram_id,request_type,status,amount_bot,amount_cash,details
        FROM cash_requests WHERE id=%s FOR UPDATE
        """, (cash_request_id,))
    request = cur.fetchone()
    if not request or request["request_type"] != "topup":
        raise ValueError("topup_request_not_found")
    if request["status"] == "completed":
        return False
    if request["status"] != "externally_verified":
        raise ValueError("topup_not_externally_verified")
    details = request.get("details") or {}
    expected_currency = str(details.get("currency") or "")
    expected_beneficiary = str(details.get("account_id") or request.get("destination") or "")
    if (Decimal(str(amount_original)) != Decimal(str(request["amount_cash"]))
            or str(currency) != expected_currency
            or str(beneficiary) != expected_beneficiary):
        raise ValueError("external_verification_mismatch")
    cur.execute(
        """
        SELECT id,verification_status,external_reference,amount_original,currency,beneficiary
        FROM cash_request_external_verifications
        WHERE cash_request_id=%s AND provider_name=%s
          AND external_reference=%s
        FOR UPDATE
        """, (cash_request_id, str(provider_name), str(external_reference)))
    verification = cur.fetchone()
    if not verification or verification["verification_status"] != "verified":
        raise ValueError("verified_external_result_required")
    if (Decimal(str(verification["amount_original"])) != Decimal(str(amount_original))
            or verification["currency"] != str(currency)
            or verification["beneficiary"] != str(beneficiary)):
        raise ValueError("external_verification_mismatch")
    cur.execute("SELECT balance FROM users WHERE telegram_id=%s FOR UPDATE", (request["telegram_id"],))
    user = cur.fetchone()
    if not user:
        raise ValueError("user_not_found")
    before = int(user["balance"] or 0)
    after = before + int(request["amount_bot"])
    cur.execute("UPDATE users SET balance=%s,updated_at=NOW() WHERE telegram_id=%s", (after, request["telegram_id"]))
    if cur.rowcount != 1:
        raise RuntimeError("topup_balance_update_failed")
    details = {
        "cash_request_id": cash_request_id,
        "provider_name": str(provider_name),
        "external_reference": str(external_reference),
    }
    cur.execute(
        """
        UPDATE transactions
           SET status='completed',balance_before=%s,balance_after=%s,
               details=details || %s,external_outcome='external_confirmed_success',
               settlement_source=%s
         WHERE cash_request_id=%s AND tx_type='bot_topup'
           AND status IN ('pending','unknown_external_result')
        RETURNING id
        """, (before, after, Json(details), str(provider_name), cash_request_id))
    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO transactions
                (telegram_id,tx_type,amount,status,method,details,balance_before,balance_after,cash_request_id,external_outcome,settlement_source)
            VALUES (%s,'bot_topup',%s,'completed','sham_syp',%s,%s,%s,%s,'external_confirmed_success',%s)
            ON CONFLICT (cash_request_id,tx_type) DO NOTHING
            RETURNING id
            """, (request["telegram_id"], request["amount_bot"], Json(details), before, after, cash_request_id, str(provider_name)))
        if not cur.fetchone():
            raise ValueError("topup_already_settled")
    cur.execute("UPDATE cash_requests SET status='completed',external_outcome='external_confirmed_success',updated_at=NOW() WHERE id=%s AND status='externally_verified'", (cash_request_id,))
    if cur.rowcount != 1:
        raise RuntimeError("topup_settlement_rowcount")
    record_financial_audit(
        cur, actor_id, actor_type, "topup_settlement", cash_request_id,
        "topup", "externally_verified", "completed", "settle",
        "verified_external_provider_result", details.get("operation_ref"),
        str(external_reference), {"provider_name": str(provider_name)},
    )
    return True


def create_method_snapshot(cur, cash_request_id, method_id, direction_id, asset_id, snapshot):
    cur.execute(
        """
        INSERT INTO cash_request_method_snapshots
            (cash_request_id, payment_method_id, payment_method_direction_id, asset_id, snapshot)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (cash_request_id) DO NOTHING
        RETURNING id
        """,
        (cash_request_id, method_id, direction_id, asset_id, snapshot),
    )
    row = cur.fetchone()
    return int(row["id"]) if row else None


def _lock_withdrawal(cur, cash_request_id):
    cur.execute("SELECT status,reservation_id,request_type,telegram_id FROM cash_requests WHERE id=%s FOR UPDATE", (cash_request_id,))
    request = cur.fetchone()
    if not request or request["request_type"] != "withdraw":
        raise ValueError("withdraw_request_not_found")
    if not request["reservation_id"]:
        raise ValueError("withdrawal_reservation_required")
    cur.execute(
        "SELECT id,telegram_id,amount_bot,status AS reservation_status,external_outcome FROM balance_reservations WHERE id=%s AND cash_request_id=%s FOR UPDATE",
        (request["reservation_id"], cash_request_id),
    )
    reservation = cur.fetchone()
    if not reservation or reservation["telegram_id"] != request["telegram_id"]:
        raise ValueError("withdrawal_reservation_mismatch")
    return {
        "reservation_id": reservation["id"],
        "telegram_id": reservation["telegram_id"],
        "amount_bot": reservation["amount_bot"],
        "reservation_status": reservation["reservation_status"],
        "external_outcome": reservation["external_outcome"],
        "request_status": request["status"],
        "request_reservation_id": request["reservation_id"],
        "request_type": request["request_type"],
    }


def transition_withdrawal(cur, cash_request_id, target_status):
    """Atomically move a reserved withdrawal through its approval/payout states."""
    allowed = {
        "approved": {"funds_reserved"},
        "payout_in_progress": {"approved"},
    }
    if target_status not in allowed:
        raise ValueError("invalid_withdrawal_transition")
    row = _lock_withdrawal(cur, cash_request_id)
    if row["request_status"] not in allowed[target_status]:
        raise ValueError("invalid_withdrawal_previous_state")
    if row["reservation_status"] != "held" or row["external_outcome"] != "unknown_external_result":
        raise ValueError("withdrawal_reservation_not_held")
    cur.execute(
        """
        UPDATE cash_requests
           SET status=%s,updated_at=NOW()
         WHERE id=%s AND request_type='withdraw' AND status=%s
           AND reservation_id=%s
         RETURNING id
        """,
        (target_status, cash_request_id, row["request_status"], row["reservation_id"]),
    )
    if cur.rowcount != 1:
        raise RuntimeError("withdrawal_transition_rowcount")
    return True


def approve_withdrawal(cur, cash_request_id):
    return transition_withdrawal(cur, cash_request_id, "approved")


def start_withdrawal_payout(cur, cash_request_id):
    return transition_withdrawal(cur, cash_request_id, "payout_in_progress")


def record_external_outcome(cur, cash_request_id, outcome, external_reference=None):
    """Bind a confirmed/unknown external outcome before any local settlement."""
    allowed = {
        "external_confirmed_success",
        "external_confirmed_failure",
        "unknown_external_result",
    }
    if outcome not in allowed:
        raise ValueError("invalid_external_outcome")
    cur.execute(
        "SELECT id,status,external_outcome FROM balance_reservations WHERE cash_request_id=%s FOR UPDATE",
        (cash_request_id,),
    )
    reservation = cur.fetchone()
    if not reservation:
        raise ValueError("reservation_not_found")
    if reservation["status"] in {"settled", "released"}:
        if reservation["external_outcome"] == outcome:
            return False
        raise ValueError("external_outcome_already_final")
    if reservation["external_outcome"] != "unknown_external_result" and reservation["external_outcome"] != outcome:
        raise ValueError("external_outcome_conflict")
    cur.execute(
        "UPDATE balance_reservations SET external_outcome=%s,external_reference=COALESCE(%s,external_reference),updated_at=NOW() WHERE id=%s AND status IN ('held','unknown_external_result') RETURNING id",
        (outcome, external_reference, reservation["id"]),
    )
    if cur.rowcount != 1:
        raise RuntimeError("external_outcome_reservation_rowcount")
    cur.execute(
        "UPDATE cash_requests SET external_outcome=%s,updated_at=NOW() WHERE id=%s AND status IN ('funds_reserved','approved','payout_in_progress','unknown_external_result') RETURNING id",
        (outcome, cash_request_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError("external_outcome_request_rowcount")
    return True


def reserve_withdrawal(cur, cash_request_id, telegram_id, amount_bot):
    """Atomically reserve available customer balance for one withdrawal request."""
    amount_bot = int(amount_bot)
    if amount_bot <= 0:
        raise ValueError("amount_bot must be positive")
    cur.execute("SELECT id,status,request_type FROM cash_requests WHERE id=%s FOR UPDATE", (cash_request_id,))
    request = cur.fetchone()
    if not request or request["request_type"] != "withdraw":
        raise ValueError("withdraw_request_not_found")
    if request["status"] != "pending":
        raise ValueError("withdraw_request_not_pending")
    cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s FOR UPDATE", (telegram_id,))
    user = cur.fetchone()
    if not user:
        raise ValueError("user_not_found")
    available = int(user["balance"] or 0) - int(user["reserved_balance"] or 0)
    if available < amount_bot:
        raise ValueError("insufficient_available_balance")
    cur.execute(
        """
        INSERT INTO balance_reservations(cash_request_id,telegram_id,amount_bot,status)
        VALUES(%s,%s,%s,'held')
        ON CONFLICT (cash_request_id) DO NOTHING
        RETURNING id
        """,
        (cash_request_id, telegram_id, amount_bot),
    )
    reservation = cur.fetchone()
    if not reservation:
        raise ValueError("withdrawal_already_reserved")
    cur.execute("UPDATE users SET reserved_balance=reserved_balance+%s,updated_at=NOW() WHERE telegram_id=%s", (amount_bot, telegram_id))
    if cur.rowcount != 1:
        raise RuntimeError("reserved_balance_update_failed")
    cur.execute("UPDATE cash_requests SET status='funds_reserved',reservation_id=%s,updated_at=NOW() WHERE id=%s", (reservation["id"], cash_request_id))
    if cur.rowcount != 1:
        raise RuntimeError("cash_request_reservation_update_failed")
    return int(reservation["id"])


def release_withdrawal(cur, cash_request_id, final_status):
    """Release a held reservation; never release an unknown external result."""
    if final_status not in {"rejected", "failed"}:
        raise ValueError("release_requires_confirmed_non_success")
    cur.execute("SELECT id,telegram_id,amount_bot,status,external_outcome FROM balance_reservations WHERE cash_request_id=%s FOR UPDATE", (cash_request_id,))
    reservation = cur.fetchone()
    if not reservation:
        raise ValueError("reservation_not_found")
    if reservation["external_outcome"] != "external_confirmed_failure":
        raise ValueError("confirmed_external_failure_required")
    if reservation["status"] not in {"held", "unknown_external_result"}:
        return False
    cur.execute("SELECT reserved_balance FROM users WHERE telegram_id=%s FOR UPDATE", (reservation["telegram_id"],))
    user = cur.fetchone()
    if not user or int(user["reserved_balance"] or 0) < int(reservation["amount_bot"]):
        raise RuntimeError("reserved_balance_inconsistent")
    cur.execute("UPDATE users SET reserved_balance=reserved_balance-%s,updated_at=NOW() WHERE telegram_id=%s", (reservation["amount_bot"], reservation["telegram_id"]))
    cur.execute("UPDATE balance_reservations SET status='released',external_outcome='external_confirmed_failure',updated_at=NOW() WHERE id=%s AND status IN ('held','unknown_external_result') AND external_outcome='external_confirmed_failure'", (reservation["id"],))
    if cur.rowcount != 1:
        raise RuntimeError("reservation_release_rowcount")
    cur.execute("UPDATE cash_requests SET status=%s,external_outcome='external_confirmed_failure',updated_at=NOW() WHERE id=%s AND status IN ('funds_reserved','approved','payout_in_progress','unknown_external_result') RETURNING id", (final_status, cash_request_id))
    if cur.rowcount != 1:
        raise RuntimeError("cash_request_release_rowcount")
    return True


def mark_withdrawal_unknown(cur, cash_request_id, reason):
    """Preserve the reservation when an external payout outcome is ambiguous."""
    reason = str(reason or "external outcome unknown").strip()[:500]
    reconciliation_key = "withdrawal:%s:unknown:%s" % (
        int(cash_request_id),
        hashlib.sha256(reason.encode("utf-8")).hexdigest(),
    )
    cur.execute("UPDATE balance_reservations SET status='unknown_external_result',external_outcome='unknown_external_result',updated_at=NOW() WHERE cash_request_id=%s AND status='held'", (cash_request_id,))
    if cur.rowcount != 1:
        cur.execute("SELECT status,external_outcome FROM balance_reservations WHERE cash_request_id=%s FOR UPDATE", (cash_request_id,))
        reservation = cur.fetchone()
        if reservation and reservation["status"] == "unknown_external_result" and reservation["external_outcome"] == "unknown_external_result":
            cur.execute("SELECT 1 FROM financial_reconciliations WHERE cash_request_id=%s AND reconciliation_key=%s", (cash_request_id, reconciliation_key))
            if cur.fetchone():
                return False
        raise RuntimeError("reservation_unknown_rowcount")
    cur.execute("UPDATE cash_requests SET status='unknown_external_result',external_outcome='unknown_external_result',updated_at=NOW() WHERE id=%s AND status IN ('funds_reserved','approved','payout_in_progress') RETURNING id", (cash_request_id,))
    if cur.rowcount != 1:
        raise RuntimeError("cash_request_unknown_rowcount")
    cur.execute("""
        INSERT INTO financial_reconciliations
            (cash_request_id,reservation_id,reconciliation_key,external_status,local_status,reason)
        SELECT %s,id,%s,'unknown','unknown_external_result',%s
        FROM balance_reservations
        WHERE cash_request_id=%s
        ON CONFLICT DO NOTHING
    """, (cash_request_id, reconciliation_key, reason, cash_request_id))
    return cur.rowcount == 1


def settle_withdrawal(cur, cash_request_id, external_reference=None):
    """Atomically settle one confirmed payout and consume the held balance."""
    cur.execute("SELECT id,telegram_id,amount_bot,status,external_outcome FROM balance_reservations WHERE cash_request_id=%s FOR UPDATE", (cash_request_id,))
    reservation = cur.fetchone()
    if not reservation:
        raise ValueError("reservation_not_found")
    if reservation["status"] == "settled":
        return False
    if reservation["status"] not in {"held", "unknown_external_result"} or reservation["external_outcome"] != "external_confirmed_success":
        raise ValueError("reservation_not_settleable")
    cur.execute("SELECT balance,reserved_balance FROM users WHERE telegram_id=%s FOR UPDATE", (reservation["telegram_id"],))
    user = cur.fetchone()
    if not user or int(user["balance"] or 0) < int(reservation["amount_bot"]):
        raise RuntimeError("balance_inconsistent")
    cur.execute("UPDATE users SET balance=balance-%s,reserved_balance=reserved_balance-%s,updated_at=NOW() WHERE telegram_id=%s", (reservation["amount_bot"], reservation["amount_bot"], reservation["telegram_id"]))
    if cur.rowcount != 1:
        raise RuntimeError("withdrawal_balance_update_failed")
    cur.execute("UPDATE balance_reservations SET status='settled',external_outcome='external_confirmed_success',external_reference=%s,updated_at=NOW() WHERE id=%s AND status IN ('held','unknown_external_result') AND external_outcome='external_confirmed_success'", (external_reference, reservation["id"]))
    if cur.rowcount != 1:
        raise RuntimeError("reservation_settle_rowcount")
    cur.execute("UPDATE cash_requests SET status='completed',external_outcome='external_confirmed_success',updated_at=NOW() WHERE id=%s AND status IN ('funds_reserved','approved','payout_in_progress','unknown_external_result') AND external_outcome='external_confirmed_success' RETURNING id", (cash_request_id,))
    if cur.rowcount != 1:
        raise RuntimeError("cash_request_settle_rowcount")
    return True
