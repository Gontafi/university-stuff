import unittest

from metrics import summarize


class MetricsTest(unittest.TestCase):
    def raw(self, states):
        return {"environment": "test fixture", "mode": "baseline", "scenario": "outage", "duration_s": 10,
                "injected_at_s": 2, "cleared_at_s": 5, "observations": {}, "consistent": True,
                "samples": [{"t": i * 2, "end_t": i * 2 + 0.1, "latency_s": 0.1, "kind": "probe",
                             "http_ok": ok, "sla_ok": ok, "degraded": False, "recovered": False}
                            for i, ok in enumerate(states)]}

    def test_outage_interval_and_denominators(self):
        result = summarize(self.raw([True, False, False, True, True]))
        self.assertEqual(result["failed_requests"], 2)
        self.assertEqual(result["downtime_s"], 4)
        self.assertEqual(result["sampled_time_availability"], 0.6)
        self.assertEqual(result["mttf_observed_s"], 2)
        self.assertIsNone(result["mtbf_observed_s"])
        self.assertEqual(result["exposure_s_per_failure"], 10)
        self.assertEqual(result["mttr_observed_s"], 4)
        self.assertAlmostEqual(result["failure_rate_per_uptime_s"], 1 / 6)

    def test_unfinished_recovery_is_censored(self):
        result = summarize(self.raw([True, True, True, False, False]))
        self.assertEqual(result["right_censored_episodes"], 1)
        self.assertIsNone(result["mttr_observed_s"])
        self.assertIsNone(result["recovery_after_clear_s"])

    def test_zero_failures_do_not_prove_infinite_mttf(self):
        result = summarize(self.raw([True] * 5))
        self.assertEqual(result["sampled_time_availability"], 1)
        self.assertIsNone(result["mttf_observed_s"])
        self.assertIsNone(result["mtbf_observed_s"])


if __name__ == "__main__":
    unittest.main()
