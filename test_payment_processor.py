"""Tests for payment_processor.py. Run: python3 -m unittest -v test_payment_processor"""

import unittest

from payment_processor import (
    TRANSACTIONS, NetworkError, PaymentProcessor, Transaction, run_baseline,
)


def ft_run(transactions=TRANSACTIONS, **kwargs):
    return PaymentProcessor("test", backoff=0, **kwargs).run(transactions)


class BaselineTest(unittest.TestCase):
    def test_stops_at_first_failure(self):
        p = PaymentProcessor("test", retries=False, checkpoints=False, backoff=0)
        with self.assertRaises(NetworkError):
            run_baseline(TRANSACTIONS, p)
        self.assertEqual(p.attempted, 2)
        self.assertEqual(p.state["success"], 1)
        self.assertEqual(p.state["total"], 12000)


class ExceptionHandlingTest(unittest.TestCase):
    def test_batch_continues_after_failures(self):
        p = ft_run(retries=False, checkpoints=False)
        self.assertEqual(len(p.status), 15)
        self.assertEqual(p.state["success"], 8)
        failed = sorted(t for t, s in p.status.items() if s == "FAILED")
        self.assertEqual(failed, ["T002", "T004", "T006", "T008", "T010", "T012", "T015"])

    def test_without_rollback_failed_rows_leak_into_db(self):
        # this is the problem found in Part B: the next COMMIT also saves the failed INSERT
        p = ft_run(retries=False, checkpoints=False)
        self.assertIn("T006", p.db.rows())
        self.assertFalse(p.consistency()["ok"])

    def test_validation_error_is_not_retried(self):
        p = ft_run([Transaction("T100", -500, "None")])
        self.assertEqual(p.status["T100"], "FAILED")
        self.assertEqual(p.retry_count, 0)


class RetryTest(unittest.TestCase):
    def test_attempts_follow_fault_rules(self):
        p = ft_run()
        attempts = {}
        for row in p.attempt_log:
            attempts[row["txn"]] = row["attempt"]
        self.assertEqual(attempts["T002"], 2)    # Network: fail, success
        self.assertEqual(attempts["T004"], 3)    # Timeout: fail, fail, success
        self.assertEqual(attempts["T006"], 3)    # Database: fail, fail, fail
        self.assertEqual(attempts["T001"], 1)
        self.assertEqual(p.retry_count, 11)
        self.assertEqual(len(p.attempt_log), 26)

    def test_final_status(self):
        p = ft_run()
        self.assertEqual(p.state["success"], 13)
        self.assertEqual(p.status["T004"], "SUCCESS")
        self.assertEqual(p.status["T006"], "ROLLED BACK")
        self.assertEqual(p.status["T012"], "ROLLED BACK")


class CheckpointRollbackTest(unittest.TestCase):
    def test_checkpoints_every_five_successes(self):
        p = ft_run()
        cps = [(c["name"], c["state"]["success"], c["state"]["total"], c["last_txn"]) for c in p.checkpoints]
        self.assertEqual(cps, [
            ("CP0", 0, 0, None),
            ("CP1", 5, 103000, "T005"),
            ("CP2", 10, 214000, "T011"),
            ("CP3", 13, 287000, "T015"),
        ])

    def test_checkpoint_is_not_changed_by_later_work(self):
        p = ft_run(TRANSACTIONS[:5])
        p.state["committed"].append("T999")
        self.assertNotIn("T999", p.checkpoints[1]["state"]["committed"])

    def test_rollback_keeps_state_db_and_gateway_equal(self):
        p = ft_run()
        self.assertEqual(p.rollback_count, 2)
        self.assertEqual(p.consistency(), {"state_total": 287000, "db_total": 287000,
                                           "gateway_net": 287000, "ok": True})
        self.assertNotIn("T006", p.db.rows())

    def test_rollback_replays_rows_committed_after_checkpoint(self):
        txns = [Transaction(f"T{i:03d}", 1000, "None") for i in range(1, 8)]
        txns.append(Transaction("T008", 5000, "Database"))
        p = ft_run(txns)
        self.assertEqual(p.state["success"], 7)
        self.assertEqual(p.state["total"], 7000)
        self.assertTrue(p.consistency()["ok"])


class IdempotencyTest(unittest.TestCase):
    def test_keys_prevent_double_charge_after_timeout(self):
        with_keys = ft_run()
        without_keys = ft_run(use_idempotency_keys=False)
        self.assertEqual(with_keys.gateway.net_charged(), 287000)
        self.assertEqual(without_keys.gateway.net_charged() - 287000, 190000)

    def test_second_run_of_same_batch_does_not_charge_again(self):
        p = ft_run()
        p.run(TRANSACTIONS)
        # 13 already recorded -> skipped; T006 and T012 now pass (DB is back)
        self.assertEqual(p.state["success"], 15)
        self.assertEqual(p.consistency()["gateway_net"], 437000)
        self.assertTrue(p.consistency()["ok"])


if __name__ == "__main__":
    unittest.main()
