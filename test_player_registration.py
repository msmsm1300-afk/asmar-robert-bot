import os
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("BRIDGE_SHARED_SECRET", "test-secret")

from app import enqueue_player_registration, generated_player_email


class PlayerRegistrationTests(unittest.TestCase):
    def test_generated_email_is_stable_and_not_customer_input(self):
        email = generated_player_email("Ahmad129", 123456)
        self.assertEqual(email, "ahmad129.123456@asmarrobert.example")

    def test_registration_requires_postgres_queue(self):
        result = enqueue_player_registration(123456, "Ahmad129", "safe-password")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "postgres_required")


if __name__ == "__main__":
    unittest.main()
