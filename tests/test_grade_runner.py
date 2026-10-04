"""Real-process checks for grading deadlines and conservative failure handling."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_eval.grade_runner import evaluate_bounded


CASE = {"case_id": "bounded-case", "business": "support", "input": "question",
        "expected_output": "answer"}
EPISODE = {"episode_id": "bounded-episode", "case_id": "bounded-case", "agent_version": "v1",
           "status": "completed", "events": [], "output": "answer", "cost": None, "latency_ms": 3}


class BoundedGraderTests(unittest.TestCase):
    def invoke(self, source, case=None, episode=None, timeout=2):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "custom_grader.py").write_text(source, encoding="utf-8")
            return evaluate_bounded(copy.deepcopy(case or CASE), copy.deepcopy(episode or EPISODE),
                                    {"kind": "python", "callable": "custom_grader:grade"},
                                    working_directory=directory, timeout=timeout)

    def test_deterministic_rule_runs_successfully(self):
        case = dict(CASE, checks=[{"id": "contains", "kind": "contains",
                                  "path": "output", "expected": "answer"}])
        grade = evaluate_bounded(case, EPISODE, timeout=2)
        self.assertEqual(grade["status"], "pass")
        self.assertTrue(grade["outcome"])
        self.assertEqual({check["id"] for check in grade["checks"]},
                         {"expected_output", "check:contains"})
        self.assertGreater(grade["grading_latency_ms"], 0)
        self.assertIsNone(grade["cost"])

    def test_custom_grader_sees_private_oracle_without_polluting_stdout(self):
        result = self.invoke(
            "import os\nprint('PRIVATE_IMPORT_LOG')\nos.write(1,b'NATIVE_IMPORT_LOG')\n"
            "def grade(case,episode):\n"
            " print('PRIVATE_FUNCTION_LOG')\n os.write(1,b'NATIVE_FUNCTION_LOG')\n"
            " return [{'id':'custom','status':'pass' if case['expected_output']==episode['output']"
            " else 'fail','evidence':['independent oracle checked']}]\n")
        self.assertEqual(result["status"], "pass")
        self.assertIn("plugin:custom", [check["id"] for check in result["checks"]])
        self.assertNotIn("LOG", json.dumps(result))

    def test_hanging_custom_grader_becomes_unknown(self):
        result = self.invoke("import time\ndef grade(case,episode):\n time.sleep(5)\n", timeout=0.2)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["outcome"])
        self.assertEqual(result["grader"]["error_type"], "timeout")
        self.assertLess(result["grading_latency_ms"], 2000)

    def test_timeout_cannot_hide_forbidden_action(self):
        target = dict(CASE, forbidden_actions=["delete_all"])
        observed = dict(EPISODE, events=[{"action": "delete_all"}])
        result = self.invoke("import time\ndef grade(case,episode):\n time.sleep(5)\n",
                             target, observed, timeout=0.2)
        self.assertEqual(result["status"], "fail")
        self.assertIn("forbidden_action:delete_all", result["violations"])
        self.assertEqual(result["grader"]["error_type"], "timeout")

    def test_timeout_cannot_hide_established_business_state_failure(self):
        target = dict(CASE, expected_state={"refunded": 10})
        observed = dict(EPISODE, final_state={"refunded": 20})
        result = self.invoke("import time\ndef grade(case,episode):\n time.sleep(5)\n",
                             target, observed, timeout=0.2)
        self.assertEqual(result["status"], "fail")
        self.assertIs(result["outcome"], False)
        self.assertIn("business state mismatch", result["evidence"])

    def test_catastrophic_regex_is_bounded(self):
        target = dict(CASE, checks=[{"id": "pattern", "kind": "regex", "path": "output",
                                    "expected": "^(a+)+$"}])
        del target["expected_output"]
        observed = dict(EPISODE, output="a" * 40 + "!")
        result = evaluate_bounded(target, observed, timeout=0.2)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["grader"]["error_type"], "timeout")

    def test_worker_exit_is_unknown_and_diagnostics_are_not_leaked(self):
        result = self.invoke("import os\ndef grade(case,episode):\n"
                             " os.write(2,b'PRIVATE_API_KEY')\n os._exit(1)\n")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["grader"]["error_type"], "worker_exit")
        self.assertNotIn("PRIVATE_API_KEY", json.dumps(result))

    def test_clean_exit_without_json_is_unknown(self):
        result = self.invoke("import os\ndef grade(case,episode):\n os._exit(0)\n")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["grader"]["error_type"], "grader_contract")

    def test_missing_model_key_abstains_without_external_service(self):
        key_name = "AGENT_EVAL_BOUNDED_MISSING_TEST_KEY"
        config = {"kind": "llm", "base_url": "http://127.0.0.1:1/v1", "model": "local-test",
                  "version": "test-v1", "api_key_env": key_name}
        target = dict(CASE, rubric=[{"id": "useful", "description": "The answer is useful"}])
        with patch.dict(os.environ, {key_name: ""}):
            result = evaluate_bounded(target, EPISODE, config, timeout=2)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("judge unavailable: missing configured API key", result["evidence"])

    def test_literal_secret_config_is_rejected_without_reporting_value(self):
        result = evaluate_bounded(CASE, EPISODE, {"kind": "llm", "api_key": "PRIVATE_SECRET"})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["grader"]["error_type"], "grader_contract")
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))

    def test_failed_grading_preserves_infrastructure_status_and_observed_violation(self):
        observed = dict(EPISODE, status="infra_error")
        result = evaluate_bounded(CASE, observed, {"kind": "invalid"})
        self.assertEqual(result["status"], "infra_error")
        observed["events"] = [{"action": "delete_all"}]
        target = dict(CASE, forbidden_actions=["delete_all"])
        result = evaluate_bounded(target, observed, {"kind": "invalid"})
        self.assertEqual(result["status"], "fail")
        self.assertIn("forbidden_action:delete_all", result["violations"])


if __name__ == "__main__":
    unittest.main()
