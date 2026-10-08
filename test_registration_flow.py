import inspect
import unittest

import app


class RegistrationFlowTests(unittest.TestCase):
    def test_default_mode_is_automatic(self):
        self.assertEqual(app.registration_mode(), "auto")

    def test_manual_mode_is_explicit_only(self):
        original = app.REGISTRATION_MODE
        try:
            app.REGISTRATION_MODE = "manual"
            self.assertEqual(app.registration_mode(), "manual")
            self.assertIn("إجراء إداري", app.registration_status_message("admin_pending"))
        finally:
            app.REGISTRATION_MODE = original

    def test_pending_and_running_are_customer_waiting_states(self):
        self.assertIn("جاري إنشاء", app.registration_status_message("pending"))
        self.assertIn("جاري إنشاء", app.registration_status_message("running"))

    def test_database_initialization_never_cancels_registration_jobs(self):
        source = inspect.getsource(app.ensure_db)
        self.assertNotIn("UPDATE bridge_jobs\n                SET status='cancelled'", source)
        self.assertNotIn("SET ichancy_creation_status='admin_pending'", source)
        self.assertIn("legacy_manual_registration_mode", source)


if __name__ == "__main__":
    unittest.main()
