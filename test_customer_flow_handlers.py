import os
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("BRIDGE_SHARED_SECRET", "test-secret")

import app


class CustomerFlowHandlerTests(unittest.TestCase):
    def test_main_buttons_have_dispatcher_handlers(self):
        expected = {
            "account": app.show_account,
            "topup": app.show_topup,
            "withdraw_bot": app.show_withdraw_bot,
            "ichancy_deposit": app.show_ichancy_deposit,
            "ichancy_withdraw": app.show_ichancy_withdraw,
            "history": app.show_history,
            "offers": app.show_offers,
            "referrals": app.show_referrals,
            "gift": app.show_gift,
            "support": app.show_support,
            "terms": app.show_terms,
        }
        keyboard_callbacks = {
            button["callback_data"]
            for row in app.main_inline_keyboard()["inline_keyboard"]
            for button in row
            if "callback_data" in button
        }
        self.assertEqual(set(expected), keyboard_callbacks)
        source = __import__("inspect").getsource(app.handle_callback)
        for callback in expected:
            self.assertIn(f'data == "{callback}"', source)

    def test_unknown_ichancy_results_are_not_reported_as_final_failures(self):
        deposit = app._customer_job_outcome_message("deposit", "unknown_external_result")
        withdraw = app._customer_job_outcome_message("withdraw", "unknown_external_result")
        self.assertIn("غير محسومة", deposit)
        self.assertIn("المصالحة", withdraw)
        self.assertIn("لم يُخصم", deposit)
        self.assertIn("لم تتم إضافة", withdraw)

    def test_customer_job_messages_do_not_include_provider_error_text(self):
        for job_type in ("deposit", "withdraw", "get_balance"):
            message = app._customer_job_outcome_message(job_type, "failed")
            self.assertNotIn("token", message.lower())
            self.assertNotIn("cookie", message.lower())
            self.assertNotIn("http_", message.lower())

    def test_financial_transition_helpers_are_not_changed_by_customer_messages(self):
        with open("app.py", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("settle_withdrawal(cur", source)
        self.assertIn("release_withdrawal(cur", source)
        self.assertIn("mark_withdrawal_unknown(cur", source)
        self.assertIn("UPDATE users SET balance", source)


if __name__ == "__main__":
    unittest.main()
