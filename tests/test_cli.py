import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-m", "agent_eval", *args],
                              text=True, capture_output=True, timeout=15)

    def test_three_schemes_feedback_loop_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.invoke("demo", "--out", directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            summary = json.loads(first.stdout)
            self.assertEqual(summary["offline_gate"], "INCONCLUSIVE")
            self.assertEqual(summary["unsafe_gate"], "BLOCK")
            self.assertGreater(summary["promotion"]["promoted"], 0)
            cases_before = Path(directory, "regression.jsonl").read_text()
            loop = json.loads(Path(directory, "loop-validation.json").read_text())
            self.assertEqual(loop["candidate"]["counts"]["fail"], 0)
            self.assertGreater(loop["baseline"]["counts"]["fail"], 0)
            second = self.invoke("demo", "--out", directory)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout)["promotion"]["promoted"], 0)
            self.assertEqual(Path(directory, "regression.jsonl").read_text(), cases_before)

    def test_ci_exit_codes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            uncertain = self.invoke("offline", "--out", str(Path(directory, "uncertain.json")))
            self.assertEqual(uncertain.returncode, 3, uncertain.stderr)
            blocked = self.invoke("offline", "--candidate", "regressed", "--out", str(Path(directory, "blocked.json")))
            self.assertEqual(blocked.returncode, 2, blocked.stderr)

    def test_initialized_project_runs_reports_and_enforces_inconclusive_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory, "project")
            initialized = self.invoke("init", str(project))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            config = json.loads(initialized.stdout)["config"]
            validated = self.invoke("validate", "--config", config)
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertEqual(json.loads(validated.stdout)["cases"], 13)

            output = project / "runs" / "first"
            completed = self.invoke("run", "--config", config, "--out", str(output))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual(result["records"], 26)
            report = json.loads(Path(result["reports"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(report["versions"]["candidate"]["summary"]["counts"],
                             {"pass": 11, "fail": 0, "unknown": 0, "infra_error": 2})
            self.assertEqual(report["gate"]["decision"], "INCONCLUSIVE")
            self.assertIn("<!doctype html>", Path(result["reports"]["html"]).read_text(encoding="utf-8").lower())

            checkpoint = (output / "records.jsonl").read_bytes()
            enforced = self.invoke("run", "--config", config, "--out", str(output),
                                   "--resume", "--enforce-gate")
            self.assertEqual(enforced.returncode, 3, enforced.stderr)
            self.assertEqual(json.loads(enforced.stdout)["gate"]["decision"], "INCONCLUSIVE")
            self.assertEqual((output / "records.jsonl").read_bytes(), checkpoint)

    def test_recorded_output_only_evaluation_reports_sparse_coverage_per_version(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cases = [{"case_id": f"c{index}", "business": "answers", "input": f"question {index}",
                      "expected_output": f"answer {index}"} for index in (1, 2, 3)]
            episodes = [
                {"episode_id": "baseline-c1", "case_id": "c1", "agent_version": "baseline",
                 "status": "completed", "events": [], "output": "answer 1"},
                {"episode_id": "candidate-c1", "case_id": "c1", "agent_version": "candidate",
                 "status": "completed", "events": [], "output": "wrong answer"},
                {"episode_id": "candidate-c2", "case_id": "c2", "agent_version": "candidate",
                 "status": "completed", "events": [], "output": "answer 2"},
            ]
            dataset, observations = directory / "cases.jsonl", directory / "episodes.jsonl"
            dataset.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
            observations.write_text("".join(json.dumps(episode) + "\n" for episode in episodes), encoding="utf-8")
            completed = self.invoke("evaluate", "--dataset", str(dataset), "--episodes", str(observations),
                                    "--out", str(directory / "report"))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(Path(json.loads(completed.stdout)["reports"]["json"]).read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["counts"],
                             {"pass": 2, "fail": 1, "unknown": 0, "infra_error": 0})
            self.assertEqual(report["summary"]["evidence_coverage"], 1)
            coverage = report["dataset_coverage"]["versions"]
            self.assertEqual(coverage["baseline"]["observed_cases"], 1)
            self.assertEqual(coverage["baseline"]["missing_cases"], 2)
            self.assertEqual(coverage["candidate"]["observed_cases"], 2)
            self.assertEqual(coverage["candidate"]["missing_cases"], 1)
            self.assertAlmostEqual(report["dataset_coverage"]["minimum_version_coverage"], 1 / 3)
            self.assertEqual(report["summary"]["cost"]["coverage"], 0)
            self.assertIsNone(report["summary"]["cost"]["cost_per_success"])
            self.assertIsNone(report["gate"])

    def test_malformed_grader_configuration_returns_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory, "project")
            initialized = self.invoke("init", str(project))
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            config_path = Path(json.loads(initialized.stdout)["config"])
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["grader"] = {"kind": "deterministic", "timeout": "forever"}
            config_path.write_text(json.dumps(config), encoding="utf-8")
            for arguments in (("validate", "--config", str(config_path)),
                              ("run", "--config", str(config_path), "--out", str(project / "bad-run"))):
                with self.subTest(command=arguments[0]):
                    completed = self.invoke(*arguments)
                    self.assertEqual(completed.returncode, 1, completed.stderr)
                    self.assertIn("grader.timeout", completed.stderr)
                    self.assertNotIn("Traceback", completed.stderr)

            grader = project / "bad-grader.json"
            grader.write_text("[]", encoding="utf-8")
            completed = self.invoke("evaluate", "--dataset", str(project / "data" / "cases.jsonl"),
                                    "--episodes", str(project / "unused-episodes.jsonl"),
                                    "--grader", str(grader), "--out", str(project / "bad-evaluate"))
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertIn("grader", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)

    def test_run_finishes_with_hanging_grader_and_retains_observed_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cases = [{"case_id": name, "business": "support", "input": {"unsafe": unsafe},
                      "expected_output": "answer", "forbidden_actions": ["delete_all"]}
                     for name, unsafe in (("safe", False), ("unsafe", True))]
            (directory / "cases.jsonl").write_text("".join(json.dumps(case) + "\n" for case in cases),
                                                   encoding="utf-8")
            (directory / "custom_agent.py").write_text(
                "def run(case,version,seed):\n"
                " return {'episode_id':version+':'+case['case_id'],'case_id':case['case_id'],"
                "'agent_version':version,'status':'completed','output':'answer',"
                "'events':[{'action':'delete_all'}] if case['input']['unsafe'] else []}\n",
                encoding="utf-8")
            (directory / "hanging_grader.py").write_text(
                "import time\ndef grade(case,episode):\n time.sleep(5)\n"
                " return [{'id':'unused','status':'pass','evidence':['unreachable before timeout']}]\n",
                encoding="utf-8")
            config = {"dataset": "cases.jsonl", "versions": ["candidate"],
                      "adapter": {"kind": "python", "callable": "custom_agent:run"},
                      "grader": {"kind": "python", "callable": "hanging_grader:grade", "timeout": 0.2},
                      "execution": {"workers": 2}, "gate": {"enabled": False}}
            config_path = directory / "eval.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = directory / "run"
            completed = self.invoke("run", "--config", str(config_path), "--out", str(output))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout)["records"], 2)
            records = {item["case_id"]: item for item in map(json.loads,
                       (output / "records.jsonl").read_text(encoding="utf-8").splitlines())}
            self.assertEqual(records["safe"]["grade"]["status"], "unknown")
            self.assertEqual(records["unsafe"]["grade"]["status"], "fail")
            self.assertIn("forbidden_action:delete_all", records["unsafe"]["grade"]["violations"])
            for record in records.values():
                self.assertEqual(record["grade"]["grader"]["error_type"], "timeout")
                self.assertLess(record["grade"]["grading_latency_ms"], 2000)
            self.assertEqual(json.loads((output / "manifest.json").read_text(encoding="utf-8"))["status"],
                             "completed")


if __name__ == "__main__":
    unittest.main()
