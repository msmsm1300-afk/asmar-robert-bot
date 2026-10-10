import copy
import inspect
import unittest
from pathlib import Path

import app


class MockFinancialStore:
    """Small in-memory model for the approved financial settlement contract."""

    def __init__(self):
        self.job = {
            "job_id": "job-1",
            "device_id": "phone-1",
            "attempt_id": "attempt-1",
            "status": "running",
        }
        self.transaction = {"status": "pending"}
        self.balance = 1000

    def complete(self, device_id, attempt_id, status, fail_at=None):
        snapshot = copy.deepcopy((self.job, self.transaction, self.balance))
        try:
            if self.job["device_id"] != device_id or self.job["attempt_id"] != attempt_id:
                raise ValueError("unauthorized_attempt")
            if self.job["status"] in {"succeeded", "failed"}:
                return "already_finalized"
            if self.job["status"] not in {"running", "unknown_external_result"}:
                raise ValueError("invalid_state")
            self.job["status"] = status
            if fail_at == "job":
                raise RuntimeError("job_update_failed")
            if status == "unknown_external_result":
                self.transaction["status"] = status
            elif status == "succeeded":
                if fail_at == "balance":
                    raise RuntimeError("balance_update_failed")
                self.balance -= 100
                if fail_at == "transaction":
                    raise RuntimeError("transaction_update_failed")
                self.transaction["status"] = "completed"
            else:
                self.transaction["status"] = "failed"
            return "committed"
        except Exception:
            self.job, self.transaction, self.balance = snapshot
            raise

    def mark_unknown_after_restart(self):
        if self.job["status"] == "running":
            self.job["status"] = "unknown_external_result"
            self.transaction["status"] = "unknown_external_result"


class FinancialSafetyTests(unittest.TestCase):
    def test_financial_requests_use_stable_operation_reference(self):
        source = inspect.getsource(app.enqueue_ichancy_deposit)
        source += inspect.getsource(app.enqueue_ichancy_withdraw)
        self.assertIn("operation_ref", source)
        self.assertIn("request_id = f\"ichancy-deposit:{chat_id}:{operation_ref}\"", source)
        self.assertIn("request_id = f\"ichancy-withdraw:{chat_id}:{operation_ref}\"", source)
        self.assertIn("details->>'job_id'", source)

    def test_native_service_sends_attempt_and_does_not_replay_financial_jobs(self):
        service = Path("/home/ubuntu/asmar-bridge-mobile/android/app/src/main/java/com/app/asmarbridgemobile/BridgeService.kt").read_text()
        self.assertIn('body.put("execution_attempt_id"', service)
        self.assertIn('financial && !completed', service)
        self.assertIn('if (financial) return r.put("ok", false)', service)
        self.assertIn('externalOutcome == "unknown"', service)

    def test_native_agent_contract_distinguishes_rejection_from_unknown(self):
        html = Path("/home/ubuntu/asmar-bridge-mobile/android/app/src/main/assets/bridge_agent.html").read_text()
        self.assertIn("function explicitRejection", html)
        self.assertIn("externalOutcome:'rejected'", html)
        self.assertIn("externalOutcome:'unknown'", html)
        self.assertIn("externalRequestStarted:true", html)
        self.assertIn("authExpired(x)", html)

    def test_backend_preserves_external_success_when_local_settlement_fails(self):
        source = inspect.getsource(app.bridge_complete_job)
        self.assertIn("external_succeeded_reconciliation_required", source)
        self.assertIn("SAVEPOINT financial_settlement", source)
        self.assertIn("ROLLBACK TO SAVEPOINT financial_settlement", source)
        self.assertIn("reconciliation_required", source)

    def test_late_confirmed_rejection_can_close_unknown_without_replay(self):
        source = inspect.getsource(app.bridge_complete_job)
        self.assertIn("status IN ('pending','unknown_external_result')", source)

    def test_source_has_attempt_binding_and_unknown_state(self):
        source = inspect.getsource(app.bridge_complete_job)
        self.assertIn("execution_attempt_id", source)
        self.assertIn("unknown_external_result", source)
        self.assertIn("claimed_by", source)
        self.assertIn("rowcount", source)

    def test_financial_claim_does_not_requeue_after_disconnect(self):
        source = inspect.getsource(app.bridge_next_job)
        self.assertIn("job_type IN ('deposit','withdraw')", source)
        self.assertIn("status='unknown_external_result'", source)
        self.assertIn("retry_blocked", source)

    def test_success_is_atomic_and_repeat_complete_is_noop(self):
        store = MockFinancialStore()
        self.assertEqual(store.complete("phone-1", "attempt-1", "succeeded"), "committed")
        self.assertEqual((store.job["status"], store.transaction["status"], store.balance), ("succeeded", "completed", 900))
        self.assertEqual(store.complete("phone-1", "attempt-1", "succeeded"), "already_finalized")
        self.assertEqual(store.balance, 900)

    def test_unknown_blocks_replay_but_late_same_attempt_can_settle(self):
        store = MockFinancialStore()
        self.assertEqual(store.complete("phone-1", "attempt-1", "unknown_external_result"), "committed")
        self.assertEqual((store.job["status"], store.transaction["status"], store.balance), ("unknown_external_result", "unknown_external_result", 1000))
        self.assertEqual(store.complete("phone-1", "attempt-1", "succeeded"), "committed")
        self.assertEqual((store.job["status"], store.transaction["status"], store.balance), ("succeeded", "completed", 900))

    def test_wrong_device_or_attempt_cannot_settle(self):
        store = MockFinancialStore()
        with self.assertRaises(ValueError):
            store.complete("other-phone", "attempt-1", "succeeded")
        with self.assertRaises(ValueError):
            store.complete("phone-1", "other-attempt", "succeeded")
        self.assertEqual((store.job["status"], store.transaction["status"], store.balance), ("running", "pending", 1000))

    def test_restart_marks_running_as_unknown_without_replay(self):
        store = MockFinancialStore()
        store.mark_unknown_after_restart()
        self.assertEqual(store.job["status"], "unknown_external_result")
        self.assertEqual(store.transaction["status"], "unknown_external_result")
        self.assertEqual(store.balance, 1000)

    def test_database_failure_rolls_back_job_transaction_and_balance(self):
        for failure in ("job", "transaction", "balance"):
            store = MockFinancialStore()
            before = copy.deepcopy((store.job, store.transaction, store.balance))
            with self.assertRaises(RuntimeError):
                store.complete("phone-1", "attempt-1", "succeeded", fail_at=failure)
            self.assertEqual((store.job, store.transaction, store.balance), before)

    def test_unknown_never_returns_to_pending_or_running(self):
        store = MockFinancialStore()
        store.complete("phone-1", "attempt-1", "unknown_external_result")
        self.assertNotIn(store.job["status"], {"pending", "running"})


if __name__ == "__main__":
    unittest.main()
