import copy
import types
import unittest
from unittest.mock import patch

from agent_eval.grading import evaluate


def case(**updates):
    return {"case_id": "c", "business": "qa", "input": "question", "expected_output": "answer", **updates}


def episode(**updates):
    return {"episode_id": "e", "case_id": "c", "agent_version": "v1", "status": "completed",
            "events": [], "output": "answer", "cost": 0.1, "latency_ms": 20, **updates}


class GradingTests(unittest.TestCase):
    def test_answer_only_case_can_pass_without_state_oracle(self):
        result = evaluate(case(), episode())
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["outcome"])
        self.assertEqual(result["grader_version"], "framework-grader-v1")
        self.assertNotIn("missing independent business-state oracle or observed state", result["evidence"])

    def test_explicit_null_and_missing_output_are_distinct(self):
        self.assertEqual(evaluate(case(expected_output=None), episode(output=None))["status"], "pass")
        raw = episode()
        del raw["output"]
        self.assertEqual(evaluate(case(expected_output=None), raw)["status"], "unknown")

    def test_all_oracles_must_pass_and_missing_evidence_abstains(self):
        target = case(expected_state={"count": 1})
        self.assertEqual(evaluate(target, episode(final_state={"count": 1, "extra": True}))["status"], "pass")
        self.assertEqual(evaluate(target, episode(final_state={"count": 0}))["status"], "fail")
        self.assertEqual(evaluate(target, episode())["status"], "unknown")
        self.assertEqual(evaluate(target, episode(output="wrong"))["status"], "fail")

    def test_boolean_does_not_satisfy_integer_anywhere_in_json(self):
        for expected, actual in ((1, True), ({"x": [1]}, {"x": [True]}), ([{"x": 1}], [{"x": True}])):
            with self.subTest(expected=expected):
                self.assertEqual(evaluate(case(expected_output=expected), episode(output=actual))["status"], "fail")
        result = evaluate(case(expected_state={"x": [1]}), episode(final_state={"x": [True]}))
        self.assertEqual(result["status"], "fail")
        self.assertNotIn("business state matches", result["evidence"])

    def test_dotted_checks_cover_nested_lists_and_all_comparators(self):
        checks = [{"id": "answer", "kind": "equals", "path": "output.answer", "expected": "hello world"},
                  {"id": "contains", "kind": "contains", "path": "output.answer", "expected": "world"},
                  {"id": "pattern", "kind": "regex", "path": "output.answer", "expected": "^hello"},
                  {"id": "nested", "kind": "equals", "path": "output.items.0.id", "expected": 1},
                  {"id": "state", "kind": "json_subset", "path": "final_state", "expected": {"done": True}}]
        target = case(checks=checks)
        del target["expected_output"]
        observed = episode(output={"answer": "hello world", "items": [{"id": 1}]}, final_state={"done": True, "n": 3})
        self.assertEqual(evaluate(target, observed)["status"], "pass")
        observed["output"]["items"] = []
        self.assertEqual(evaluate(target, observed)["status"], "unknown")
        observed["output"]["answer"] = "bad"
        self.assertEqual(evaluate(target, observed)["status"], "fail")

    def test_contains_array_and_object_and_type_mismatch(self):
        for actual, expected, status in (([1, 2], 2, "pass"), ([1], True, "fail"),
                                         ({"key": 1}, "key", "pass"), (7, "7", "fail")):
            target = case(checks=[{"id": "item", "kind": "contains", "path": "output", "expected": expected}])
            del target["expected_output"]
            with self.subTest(actual=actual):
                self.assertEqual(evaluate(target, episode(output=actual))["status"], status)

    def test_required_success_guard_reuses_core_trajectory_rules(self):
        target = case(required_success_before=[["authorize", "write"]])
        safe = [{"action": "authorize", "result": {"ok": True}}, {"action": "write"}]
        self.assertEqual(evaluate(target, episode(events=safe))["status"], "pass")
        unsafe = [{"action": "authorize", "result": {"ok": False}}, {"action": "write"}]
        result = evaluate(target, episode(events=unsafe))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["outcome"])
        self.assertIn("successful_guard:authorize<write", result["violations"])

    def test_forbidden_action_cannot_be_compensated_by_good_answer(self):
        result = evaluate(case(forbidden_actions=["bypass"]), episode(events=[{"action": "bypass"}]))
        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["outcome"])

    def test_infrastructure_failure_does_not_manufacture_missing_action_failure(self):
        result = evaluate(case(required_actions=["write"], required_order=[["check", "write"]]),
                          episode(status="infra_error", output="wrong"))
        self.assertEqual(result["status"], "infra_error")
        self.assertIsNone(result["outcome"])
        self.assertEqual(result["violations"], [])

    def test_observed_infra_safety_violation_remains_failure(self):
        result = evaluate(case(forbidden_actions=["bypass"]),
                          episode(status="infra_error", events=[{"action": "bypass"}]))
        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["outcome"])

    def test_resource_evidence_missing_is_unknown_exceeded_is_failure(self):
        target = case(limits={"max_cost": 1, "max_latency_ms": 50, "max_steps": 4})
        self.assertEqual(evaluate(target, episode(cost=None))["status"], "unknown")
        self.assertEqual(evaluate(target, episode(cost=2))["status"], "fail")
        self.assertEqual(evaluate(target, episode())["status"], "pass")

    def test_unconfigured_rubric_is_not_silently_skipped(self):
        result = evaluate(case(rubric=[{"id": "helpful", "description": "Helpful answer"}]), episode())
        self.assertEqual(result["status"], "unknown")
        self.assertIn("rubric requires an explicitly configured LLM judge", result["evidence"])

    def test_judge_failure_and_abstention_are_combined_with_deterministic_oracles(self):
        target = case(rubric=[{"id": "helpful", "description": "Helpful answer"}])
        for status, expected in (("pass", "pass"), ("unknown", "unknown"), ("fail", "fail")):
            with self.subTest(status=status), patch("agent_eval.grading.evaluate_rubric", return_value={
                    "checks": [{"id": "rubric:helpful", "status": status, "evidence": ["evidence"]}],
                    "judge": {"version": "judge-test-v2"}}):
                result = evaluate(target, episode(), {"kind": "llm"})
                self.assertEqual(result["status"], expected)
                self.assertIn("judge-test-v2", result["grader_version"])
                self.assertEqual(evaluate(target, episode(output="wrong"), {"kind": "llm"})["status"], "fail")

    def test_custom_grader_is_additive_and_receives_copies_of_private_oracles(self):
        target, observed = case(), episode()
        originals = copy.deepcopy((target, observed))
        def custom(private_case, private_episode):
            self.assertEqual(private_case["expected_output"], "answer")
            private_case["input"] = "mutation"
            private_episode["output"] = "mutation"
            return [{"id": "custom", "status": "pass", "evidence": "external fact checked"}]
        with patch("agent_eval.grading.importlib.import_module", return_value=types.SimpleNamespace(check=custom)):
            result = evaluate(target, observed, {"callable": "example:check", "version": "v3"})
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["grader"]["plugin"]["version"], "v3")
        self.assertEqual((target, observed), originals)

    def test_custom_grader_error_is_unknown_and_error_text_is_not_logged(self):
        with patch("agent_eval.grading.importlib.import_module", side_effect=RuntimeError("SECRET")):
            result = evaluate(case(), episode(), {"callable": "bad:check"})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["grader"]["plugin"]["error_type"], "RuntimeError")
        self.assertNotIn("SECRET", str(result))

    def test_infra_execution_does_not_call_custom_grader(self):
        with patch("agent_eval.grading.importlib.import_module") as importer:
            result = evaluate(case(), episode(status="infra_error"), {"callable": "bad:check"})
        importer.assert_not_called()
        self.assertEqual(result["status"], "infra_error")

    def test_mismatched_case_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "episode.case_id"):
            evaluate(case(), episode(case_id="different"))


if __name__ == "__main__":
    unittest.main()
