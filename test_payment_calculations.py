import unittest
from decimal import Decimal
from payment_calculations import calculate_deposit_quote, calculate_withdraw_quote, validate_dynamic_fields, ichancy_deposit_units, ichancy_withdraw_bot_units


class DynamicCalculationTests(unittest.TestCase):
    def test_syp_multiplier_bonus_fee_floor(self):
        snapshot = {
            "exchange_rate": "1", "conversion_multiplier": "100",
            "bonus_enabled": True, "bonus_percent": "10",
            "fee_type": "fixed", "fee_value": "50",
            "minimum_amount": "1", "amount_scale": 0,
        }
        result = calculate_deposit_quote("200", snapshot)
        self.assertEqual(result["base_amount"], Decimal("20000"))
        self.assertEqual(result["bonus_amount"], Decimal("2000"))
        self.assertEqual(result["fee_amount"], Decimal("50"))
        self.assertEqual(result["bot_amount"], Decimal("21950"))

    def test_withdraw_divides_multiplier_and_keeps_fx_separate(self):
        snapshot = {
            "exchange_rate": "2", "conversion_multiplier": "100",
            "bonus_enabled": False, "fee_type": "percent", "fee_value": "10",
            "minimum_amount": "20000", "maximum_amount": "50000", "amount_scale": 2,
        }
        result = calculate_withdraw_quote("20000", snapshot)
        self.assertEqual(result["base_amount"], Decimal("100"))
        self.assertEqual(result["fee_amount"], Decimal("10"))
        self.assertEqual(result["payout_amount"], Decimal("90.00"))

    def test_limits_and_floor(self):
        snapshot = {"exchange_rate": "1", "conversion_multiplier": "3", "minimum_amount": "1", "maximum_amount": "10", "amount_scale": 0}
        self.assertEqual(calculate_deposit_quote("2", snapshot)["bot_amount"], Decimal("6"))
        with self.assertRaisesRegex(ValueError, "amount_below_minimum"):
            calculate_deposit_quote("0.5", snapshot)
        with self.assertRaisesRegex(ValueError, "amount_above_maximum"):
            calculate_deposit_quote("11", snapshot)

    def test_dynamic_field_modes(self):
        fields = {"amount": "required", "operation_reference": "required", "proof_image": "optional", "note": "disabled"}
        with self.assertRaisesRegex(ValueError, "required_field:operation_reference"):
            validate_dynamic_fields(fields, {"amount": "10"})
        with self.assertRaisesRegex(ValueError, "disabled_field:note"):
            validate_dynamic_fields(fields, {"amount": "10", "operation_reference": "x", "note": "bad"})
        self.assertTrue(validate_dynamic_fields(fields, {"amount": "10", "operation_reference": "x"}))

    def test_all_supported_dynamic_fields_required_optional_disabled(self):
        keys = ("amount", "operation_reference", "proof_image", "note", "recipient_account", "recipient_name", "network")
        for key in keys:
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, f"required_field:{key}"):
                    validate_dynamic_fields({key: "required"}, {})
                self.assertTrue(validate_dynamic_fields({key: "optional"}, {}))
                with self.assertRaisesRegex(ValueError, f"disabled_field:{key}"):
                    validate_dynamic_fields({key: "disabled"}, {key: "provided"})

    def test_legacy_ichancy_conversion_is_centralized_and_single(self):
        self.assertEqual(ichancy_deposit_units(200), 20000)
        self.assertEqual(ichancy_withdraw_bot_units(20000), 200)
        with self.assertRaises(ValueError):
            ichancy_withdraw_bot_units(20001)


if __name__ == "__main__":
    unittest.main()
