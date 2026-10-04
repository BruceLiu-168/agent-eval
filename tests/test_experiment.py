from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_eval.config import load_config
from agent_eval.experiment import evaluate_recorded, run_experiment


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = self.root / "cases.jsonl"
        self.case = {"case_id": "c1", "business": "support", "input": {"text": "hello"},
                     "metadata": {"private_reference": "do not disclose"}, "expected_output": "good"}
        self.dataset.write_text(json.dumps(self.case) + "\n")
        self.config_path = self.root / "eval.json"
        self.config = {"dataset": "cases.jsonl", "versions": ["baseline", "candidate"],
                       "adapter": {"kind": "python", "callable": "agent_eval.demo_agent:run"},
                       "execution": {"workers": 1, "trials": 2}}
        self.save_config()

    def tearDown(self):
        self.temp.cleanup()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def agent(self, adapter, case, version, seed, timeout):
        self.assertNotIn("expected_output", case)
        self.assertNotIn("metadata", case)
        return {"episode_id": f"{version}:{case['case_id']}:{seed}", "case_id": case["case_id"],
                "agent_version": version, "status": "completed", "events": [],
                "output": "good" if version == "candidate" else "wrong", "cost": None,
                "latency_ms": 2, "execution_latency_ms": 3}

    def test_checkpoint_resume_never_reexecutes_saved_trials(self):
        out = self.root / "run"
        with patch("agent_eval.experiment.execute_adapter", side_effect=self.agent) as agent:
            result = run_experiment(self.config_path, out)
            self.assertEqual(agent.call_count, 4)
            resumed = run_experiment(self.config_path, out, resume=True)
            self.assertEqual(agent.call_count, 4)
        self.assertEqual(result["run_id"], resumed["run_id"])
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["versions"]["candidate"]["summary"]["unique_cases"], 1)
        self.assertEqual(report["versions"]["baseline"]["summary"]["counts"]["fail"], 2)

    def test_changed_dataset_and_corrupt_record_refuse_resume(self):
        out = self.root / "run"
        with patch("agent_eval.experiment.execute_adapter", side_effect=self.agent):
            run_experiment(self.config_path, out)
        original = self.dataset.read_text()
        self.dataset.write_text(original.replace('good', 'changed'))
        with self.assertRaisesRegex(ValueError, "changed"):
            run_experiment(self.config_path, out, resume=True)
        self.dataset.write_text(original)
        records = (out / "records.jsonl").read_text().splitlines()
        edited = json.loads(records[0])
        edited["grade"]["status"] = "pass"
        records[0] = json.dumps(edited)
        (out / "records.jsonl").write_text("\n".join(records) + "\n")
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            run_experiment(self.config_path, out, resume=True)

    def test_parallel_runs_cannot_duplicate_execution(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.agent(*args, **kwargs)
        with patch("agent_eval.experiment.execute_adapter", side_effect=blocking):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(run_experiment, self.config_path, self.root / "run")
                self.assertTrue(entered.wait(5))
                try:
                    with self.assertRaisesRegex(ValueError, "locked"):
                        run_experiment(self.config_path, self.root / "run", resume=True)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=10)["records"], 4)

    def test_partial_saved_run_resumes_only_missing_jobs(self):
        out = self.root / "run"
        with patch("agent_eval.experiment.execute_adapter", side_effect=self.agent):
            run_experiment(self.config_path, out)
        first_line = (out / "records.jsonl").read_text().splitlines()[0]
        (out / "records.jsonl").write_text(first_line + "\n")
        manifest = json.loads((out / "manifest.json").read_text())
        manifest["status"] = "interrupted"
        (out / "manifest.json").write_text(json.dumps(manifest))
        with patch("agent_eval.experiment.execute_adapter", side_effect=self.agent) as agent:
            resumed = run_experiment(self.config_path, out, resume=True)
            self.assertEqual(agent.call_count, 3)
            self.assertEqual(resumed["records"], 4)

    def test_completed_run_lost_records_refuses_reexecution(self):
        out = self.root / "run"
        with patch("agent_eval.experiment.execute_adapter", side_effect=self.agent) as agent:
            run_experiment(self.config_path, out)
            saved = (out / "records.jsonl").read_text()
            (out / "records.jsonl").write_text(saved.splitlines()[0] + "\n")
            with self.assertRaisesRegex(ValueError, "checkpoint is missing or incomplete"):
                run_experiment(self.config_path, out, resume=True)
            (out / "records.jsonl").unlink()
            with self.assertRaisesRegex(ValueError, "checkpoint is missing or incomplete"):
                run_experiment(self.config_path, out, resume=True)
            self.assertEqual(agent.call_count, 4)

    def test_existing_episodes_show_missing_dataset_cases(self):
        second = dict(self.case, case_id="c2")
        self.dataset.write_text(json.dumps(self.case) + "\n" + json.dumps(second) + "\n")
        episodes = self.root / "episodes.jsonl"
        episodes.write_text(json.dumps(self.agent({}, {"case_id": "c1"}, "candidate", 0, 30)) + "\n")
        evaluate_recorded(self.dataset, episodes, self.root / "recorded")
        report = json.loads((self.root / "recorded" / "report.json").read_text())
        coverage = report["versions"]["candidate"]["summary"]["dataset_coverage"]
        self.assertEqual(coverage["coverage"], 0.5)
        self.assertEqual(coverage["missing_cases"], 1)

    def test_invalid_adapter_version_becomes_infrastructure_error(self):
        def invalid(*args, **kwargs):
            result = self.agent(*args, **kwargs)
            result["agent_version"] = "wrong"
            return result
        with patch("agent_eval.experiment.execute_adapter", side_effect=invalid):
            run_experiment(self.config_path, self.root / "run")
        report = json.loads((self.root / "run" / "report.json").read_text())
        self.assertEqual(report["summary"]["counts"]["infra_error"], 4)

    def test_config_typos_and_invalid_numeric_options_fail_early(self):
        self.config["execution"]["workers"] = True
        self.save_config()
        with self.assertRaises(ValueError):
            load_config(self.config_path)
        self.config["execution"] = {}
        self.config["gate"] = {"enabled": True, "baseline": "baseline", "candidate": "baseline"}
        self.save_config()
        with self.assertRaises(ValueError):
            load_config(self.config_path)

    def test_no_silent_overwrite_or_nonexistent_resume(self):
        with self.assertRaisesRegex(ValueError, "existing manifest"):
            run_experiment(self.config_path, self.root / "none", resume=True)
        with patch("agent_eval.experiment.execute_adapter", side_effect=self.agent):
            run_experiment(self.config_path, self.root / "run")
        with self.assertRaisesRegex(ValueError, "already exists"):
            run_experiment(self.config_path, self.root / "run")


if __name__ == "__main__":
    unittest.main()
