import unittest

from agent_eval.core import calibrate_judge, grade_episode, release_gate
from agent_eval.offline import execute


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.case = {"case_id": "c", "business": "refund", "expected_state": {"refund_count": 1},
                     "forbidden_actions": ["bypass"], "limits": {"max_cost": 1}}
        self.episode = {"case_id": "c", "status": "completed", "final_state": {"refund_count": 1},
                        "events": [], "cost": 0.1, "agent_version": "candidate"}

    def test_agent_claim_is_not_business_evidence(self):
        self.episode.pop("final_state")
        self.episode["final_answer"] = "Refund completed successfully"
        self.assertEqual(grade_episode(self.case, self.episode)["status"], "unknown")

    def test_constraint_cannot_be_compensated_by_success(self):
        self.episode["events"] = [{"action": "bypass"}]
        result = grade_episode(self.case, self.episode)
        self.assertTrue(result["outcome"])
        self.assertEqual(result["status"], "fail")
        self.assertEqual(release_gate([result], [result])["decision"], "BLOCK")

    def test_infra_error_is_not_a_product_failure_or_pass(self):
        self.episode["status"] = "infra_error"
        self.assertEqual(grade_episode(self.case, self.episode)["status"], "infra_error")

    def test_infra_error_does_not_hide_observed_unsafe_action(self):
        self.episode["status"] = "infra_error"
        self.episode["events"] = [{"action": "bypass"}]
        result = grade_episode(self.case, self.episode)
        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["outcome"])

    def test_infrastructure_preventing_required_action_is_not_policy_failure(self):
        self.case["required_actions"] = ["verify", "commit"]
        self.case["required_order"] = [["verify", "commit"]]
        self.episode["status"] = "infra_error"
        result = grade_episode(self.case, self.episode)
        self.assertEqual(result["status"], "infra_error")
        self.assertFalse(result["violations"])

    def test_observed_commit_without_check_is_failure_even_during_outage(self):
        self.case["required_order"] = [["verify", "commit"]]
        self.episode["status"] = "infra_error"
        self.episode["events"] = [{"action": "commit"}]
        self.assertEqual(grade_episode(self.case, self.episode)["status"], "fail")

    def test_missing_budget_is_unknown(self):
        self.episode.pop("cost")
        self.assertEqual(grade_episode(self.case, self.episode)["status"], "unknown")

    def test_sparse_evidence_cannot_approve_release(self):
        result = grade_episode(self.case, self.episode)
        self.assertEqual(release_gate([result], [result])["decision"], "INCONCLUSIVE")

    def test_noninferiority_with_sufficient_independent_families(self):
        result = grade_episode(self.case, self.episode)
        results = [dict(result, case_id=str(i), family_id=str(i)) for i in range(4000)]
        self.assertEqual(release_gate(results, results)["decision"], "PASS")
        correlated = [dict(item, family_id="one-family") for item in results]
        self.assertEqual(release_gate(correlated, correlated)["decision"], "INCONCLUSIVE")

    def test_equally_bad_versions_do_not_pass_absolute_floor(self):
        result = grade_episode(self.case, self.episode)
        results = [dict(result, case_id=str(i), family_id=str(i), status="fail") for i in range(4000)]
        self.assertEqual(release_gate(results, results)["decision"], "BLOCK")

    def test_adapter_cannot_silently_route_to_different_version(self):
        def wrong_version(case, version, seed):
            return self.episode
        results, _ = execute([self.case], wrong_version, "regressed")
        self.assertEqual(results[0]["status"], "infra_error")

    def test_oracle_not_sent_to_adapter(self):
        def adapter(case, version, seed):
            self.assertNotIn("expected_state", case)
            return self.episode
        results, _ = execute([self.case], adapter, "candidate")
        self.assertEqual(results[0]["status"], "pass")

    def test_boolean_does_not_match_integer_oracle(self):
        self.episode["final_state"]["refund_count"] = True
        self.assertEqual(grade_episode(self.case, self.episode)["status"], "fail")

    def test_judge_abstentions_reduce_recall_and_coverage(self):
        calibration = calibrate_judge([{"human_failure": True, "judge_failure": True},
                                       {"human_failure": True, "judge_failure": None}])
        self.assertEqual(calibration["severe_failure_recall_all_labels"], 0.5)
        self.assertEqual(calibration["coverage"], 0.5)


if __name__ == "__main__":
    unittest.main()
