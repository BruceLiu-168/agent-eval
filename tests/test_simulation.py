"""Behavioral tests for local state transitions, faults, and scripted policies."""

import copy
import unittest

from agent_eval.simulation import load_demo_cases, run_case


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.cases = {case["case_id"]: case for case in load_demo_cases()}

    def run_demo(self, case_id, version="candidate", seed=0):
        return run_case(self.cases[case_id], version, seed)

    def assert_subset(self, expected, actual):
        for key, value in expected.items():
            self.assertIn(key, actual)
            if isinstance(value, dict):
                self.assertIsInstance(actual[key], dict)
                self.assert_subset(value, actual[key])
            else:
                self.assertIs(type(actual[key]), type(value))
                self.assertEqual(value, actual[key])

    def test_candidate_matches_independent_fixture_contracts(self):
        for case_id, case in self.cases.items():
            with self.subTest(case_id=case_id):
                episode = self.run_demo(case_id)
                self.assertTrue(case["expected_state"])
                self.assert_subset(case["expected_state"], episode["final_state"])
                actions = [event["action"] for event in episode["events"]]
                self.assertTrue(set(case["required_actions"]) <= set(actions))
                self.assertFalse(set(case["forbidden_actions"]) & set(actions))
                for before, after in case["required_order"]:
                    self.assertLess(actions.index(before), actions.index(after))

    def test_oracle_is_not_visible_to_the_scripted_agent(self):
        original = self.cases["refund_happy"]
        poisoned = copy.deepcopy(original)
        poisoned["expected_state"] = {"order": {"refunded": 999999.0}}
        poisoned["required_actions"] = ["invented_tool"]
        poisoned["forbidden_actions"] = ["commit_refund"]
        poisoned["required_order"] = [["invented_tool", "commit_refund"]]
        poisoned["required_success_before"] = [["invented_guard", "commit_refund"]]
        self.assertEqual(run_case(original, "candidate"), run_case(poisoned, "candidate"))
        del poisoned["expected_state"]
        self.assertEqual(run_case(original, "candidate"), run_case(poisoned, "candidate"))

    def test_initial_state_is_isolated_between_episodes(self):
        case = self.cases["refund_happy"]
        initial = copy.deepcopy(case)
        first = run_case(case, "candidate")
        first["final_state"]["order"]["refunded"] = 999.0
        self.assertEqual(case, initial)
        self.assertEqual(run_case(case, "candidate")["final_state"]["order"]["refunded"], 40.0)

    def test_timeout_after_commit_recovers_without_a_second_write(self):
        episode = self.run_demo("refund_timeout_after")
        self.assertEqual(episode["final_state"]["order"]["refund_count"], 1)
        commits = [e for e in episode["events"] if e["action"] == "commit_refund"]
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["result"]["error"], "timeout")
        lookup = next(e for e in episode["events"] if e["action"] == "lookup_operation")
        self.assertTrue(lookup["result"]["found"])

    def test_timeout_before_commit_retries_with_the_same_key(self):
        episode = self.run_demo("refund_timeout_before")
        commits = [e for e in episode["events"] if e["action"] == "commit_refund"]
        self.assertEqual(len(commits), 2)
        self.assertEqual(commits[0]["args"]["idempotency_key"], commits[1]["args"]["idempotency_key"])
        self.assertEqual(episode["final_state"]["order"]["refund_count"], 1)
        lookup = next(e for e in episode["events"] if e["action"] == "lookup_operation")
        self.assertFalse(lookup["result"]["found"])

    def test_baseline_exposes_double_refund_on_ambiguous_timeout(self):
        episode = self.run_demo("refund_timeout_after", "baseline")
        self.assertEqual(episode["final_state"]["order"]["refund_count"], 2)
        self.assertEqual(episode["final_state"]["order"]["refunded"], 80.0)

    def test_duplicate_delivery_is_deduplicated_across_existing_state(self):
        candidate = self.run_demo("refund_duplicate_delivery")
        baseline = self.run_demo("refund_duplicate_delivery", "baseline")
        self.assertEqual(candidate["final_state"]["order"]["refund_count"], 1)
        self.assertEqual(baseline["final_state"]["order"]["refund_count"], 2)

    def test_completed_full_refund_can_be_safely_replayed(self):
        case = copy.deepcopy(self.cases["refund_duplicate_delivery"])
        case["initial_state"]["order"]["paid"] = 40.0
        episode = run_case(case, "candidate")
        self.assertEqual(episode["final_state"]["outcome"], "succeeded")
        self.assertEqual(episode["final_state"]["order"]["refund_count"], 1)

    def test_reusing_an_idempotency_key_with_a_new_payload_is_rejected(self):
        case = copy.deepcopy(self.cases["refund_duplicate_delivery"])
        case["input"]["amount"] = 20.0
        episode = run_case(case, "candidate")
        self.assertEqual(episode["final_state"]["outcome"], "rejected")
        self.assertEqual(episode["final_state"]["order"]["refunded"], 40.0)
        self.assertTrue(any(e["result"].get("error") == "idempotency_conflict"
                            for e in episode["events"]))

    def test_unauthorized_agent_does_not_invoke_refund_mutation(self):
        episode = self.run_demo("refund_owner_denied")
        self.assertEqual(episode["final_state"]["order"]["refunded"], 0.0)
        self.assertNotIn("commit_refund", [e["action"] for e in episode["events"]])
        unsafe = self.run_demo("refund_owner_denied", "regressed")
        self.assertGreater(unsafe["final_state"]["order"]["refunded"], 0.0)

    def test_pending_approval_cannot_grant_access(self):
        candidate = self.run_demo("access_approval_pending")
        baseline = self.run_demo("access_approval_pending", "baseline")
        self.assertFalse(candidate["final_state"]["access"]["granted"])
        self.assertEqual(candidate["final_state"]["outcome"], "awaiting_approval")
        self.assertTrue(baseline["final_state"]["access"]["granted"])

    def test_approval_recovery_uses_a_separate_agent_and_trusted_service(self):
        episode = self.run_demo("access_approval_recovery")
        request_event = next(e for e in episode["events"] if e["action"] == "request_approval")
        grant_event = next(e for e in episode["events"] if e["action"] == "grant_access")
        self.assertEqual(request_event["agent_id"], "approval_agent")
        self.assertEqual(grant_event["agent_id"], "access_agent")
        self.assertTrue(episode["final_state"]["access"]["granted"])

    def test_requester_cannot_self_approve_or_spoof_their_role(self):
        case = copy.deepcopy(self.cases["access_approval_pending"])
        case["input"].update(allow_approval_request=True, approval_decision="approved")
        self.assertFalse(run_case(case, "candidate")["final_state"]["access"]["granted"])
        case = copy.deepcopy(self.cases["access_role_denied"])
        case["input"]["role"] = "finance"
        self.assertFalse(run_case(case, "candidate")["final_state"]["access"]["granted"])

    def test_successful_guard_contract_rejects_denied_or_pending_approval(self):
        from agent_eval.core import grade_episode

        case = self.cases["access_preapproved"]
        for status in ("denied", "pending"):
            with self.subTest(approval_status=status):
                episode = self.run_demo("access_preapproved")
                approval = next(e for e in episode["events"] if e["action"] == "check_approval")
                approval["result"] = {"ok": False, "approval_status": status}
                grade = grade_episode(case, episode)
                self.assertEqual(grade["status"], "fail")
                self.assertTrue(grade["violations"])

    def test_successful_guards_allow_pending_then_approved_handoff(self):
        from agent_eval.core import grade_episode

        episode = self.run_demo("access_approval_recovery")
        approvals = [e for e in episode["events"] if e["action"] == "check_approval"]
        self.assertFalse(approvals[0]["result"]["ok"])
        self.assertTrue(approvals[-1]["result"]["ok"])
        self.assertEqual(grade_episode(self.cases["access_approval_recovery"], episode)["status"], "pass")

    def test_successful_guards_preserve_ambiguous_timeout_retries(self):
        from agent_eval.core import grade_episode

        for case_id in ("refund_timeout_before", "refund_timeout_after", "access_timeout_after"):
            with self.subTest(case_id=case_id):
                self.assertEqual(grade_episode(self.cases[case_id], self.run_demo(case_id))["status"], "pass")

    def test_latest_successful_guard_is_checked_before_every_mutation(self):
        from agent_eval.core import grade_episode

        case = self.cases["refund_timeout_before"]
        episode = self.run_demo("refund_timeout_before")
        commits = [i for i, event in enumerate(episode["events"]) if event["action"] == "commit_refund"]
        check = copy.deepcopy(next(e for e in episode["events"] if e["action"] == "check_refund_policy"))
        check["result"] = {"ok": False, "reason": "policy_revoked_before_retry"}
        episode["events"].insert(commits[1], check)
        grade = grade_episode(case, episode)
        self.assertEqual(grade["status"], "fail")
        self.assertTrue(grade["violations"])

    def test_persistent_outage_is_bounded_and_distinct_from_task_failure(self):
        for case_id, mutation in [("refund_tool_error", "commit_refund"),
                                  ("access_tool_error", "grant_access")]:
            with self.subTest(case_id=case_id):
                episode = self.run_demo(case_id)
                self.assertEqual(episode["status"], "infra_error")
                self.assertEqual(sum(e["action"] == mutation for e in episode["events"]), 3)
                self.assertTrue(any(e["args"].get("to_agent") == "human_support"
                                    for e in episode["events"] if e["action"] == "handoff"))

    def test_seeded_metrics_and_episode_identity_are_reproducible(self):
        first = self.run_demo("refund_happy", seed=11)
        self.assertEqual(first, self.run_demo("refund_happy", seed=11))
        self.assertNotEqual(first["episode_id"], self.run_demo("refund_happy", seed=12)["episode_id"])
        self.assertAlmostEqual(first["cost"], sum(e["cost"] for e in first["events"]))
        self.assertEqual(first["latency_ms"], sum(e["latency_ms"] for e in first["events"]))

    def test_unknown_versions_and_businesses_fail_explicitly(self):
        with self.assertRaises(ValueError):
            self.run_demo("refund_happy", version="typo")
        case = copy.deepcopy(self.cases["refund_happy"])
        case["business"] = "undefined"
        with self.assertRaises(ValueError):
            run_case(case, "candidate")


if __name__ == "__main__":
    unittest.main()
