"""Deterministic quotes for dynamic payment-method snapshots."""
from decimal import Decimal, ROUND_FLOOR
import re

ICHANCY_CONVERSION_MULTIPLIER = Decimal("100")


def normalize_operation_reference(value):
    """Return a stable provider reference while preserving meaningful symbols."""
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    if not normalized:
        raise ValueError("operation_reference_required")
    if len(normalized) > 120:
        raise ValueError("operation_reference_too_long")
    return normalized.casefold()


def _d(value):
    return Decimal(str(value))


def _floor(value, scale=0):
    quantum = Decimal(1).scaleb(-int(scale))
    return _d(value).quantize(quantum, rounding=ROUND_FLOOR)


def _fees_and_bonus(base, snapshot):
    bonus = base * _d(snapshot.get("bonus_percent", 0)) / Decimal(100) if snapshot.get("bonus_enabled") else Decimal(0)
    subtotal = base + bonus
    fee_type = snapshot.get("fee_type", "none")
    fee_value = _d(snapshot.get("fee_value", 0))
    fee = fee_value if fee_type == "fixed" else (subtotal * fee_value / Decimal(100) if fee_type == "percent" else Decimal(0))
    return bonus, fee, subtotal - fee


def _check_limits(amount, snapshot):
    amount = _d(amount)
    minimum = _d(snapshot.get("minimum_amount", 0))
    maximum = snapshot.get("maximum_amount")
    if amount < minimum:
        raise ValueError("amount_below_minimum")
    if maximum not in (None, "") and amount > _d(maximum):
        raise ValueError("amount_above_maximum")


def calculate_deposit_quote(amount, snapshot):
    amount = _d(amount)
    _check_limits(amount, snapshot)
    # exchange_rate and conversion_multiplier are separate configured factors;
    # neither is inferred from currency names and neither is applied twice.
    base = amount * _d(snapshot.get("exchange_rate", 1)) * _d(snapshot.get("conversion_multiplier", 1))
    bonus, fee, final = _fees_and_bonus(base, snapshot)
    return {
        "input_amount": amount,
        "base_amount": base,
        "bonus_amount": bonus,
        "fee_amount": fee,
        "bot_amount": _floor(final, 0),
        "rounding": "floor",
    }


def calculate_withdraw_quote(bot_amount, snapshot):
    bot_amount = _d(bot_amount)
    _check_limits(bot_amount, snapshot)
    multiplier = _d(snapshot.get("conversion_multiplier", 1))
    if multiplier <= 0:
        raise ValueError("invalid_conversion_multiplier")
    base = bot_amount / multiplier / _d(snapshot.get("exchange_rate", 1))
    bonus, fee, final = _fees_and_bonus(base, snapshot)
    return {
        "input_bot_amount": bot_amount,
        "base_amount": base,
        "bonus_amount": bonus,
        "fee_amount": fee,
        "payout_amount": _floor(final, int(snapshot.get("amount_scale", 0))),
        "rounding": "floor",
    }


def calculate_quote_from_snapshot(amount, snapshot, direction):
    """Recalculate an existing request using only its persisted snapshot."""
    if direction == "deposit":
        return calculate_deposit_quote(amount, snapshot)
    if direction == "withdraw":
        return calculate_withdraw_quote(amount, snapshot)
    raise ValueError("invalid_direction")


def ichancy_deposit_units(bot_amount):
    amount = _d(bot_amount)
    if amount <= 0 or amount != amount.to_integral_value():
        raise ValueError("invalid_bot_amount")
    return int(amount * ICHANCY_CONVERSION_MULTIPLIER)


def ichancy_withdraw_bot_units(ichancy_amount):
    amount = _d(ichancy_amount)
    if amount <= 0 or amount % ICHANCY_CONVERSION_MULTIPLIER:
        raise ValueError("invalid_conversion")
    return int(amount / ICHANCY_CONVERSION_MULTIPLIER)


def validate_dynamic_fields(fields, values):
    values = values or {}
    for key, mode in (fields or {}).items():
        if mode == "required" and not str(values.get(key, "")).strip():
            raise ValueError(f"required_field:{key}")
        if mode == "disabled" and key in values and str(values.get(key, "")).strip():
            raise ValueError(f"disabled_field:{key}")
    return True
