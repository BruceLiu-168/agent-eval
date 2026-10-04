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


if __name__ == "__main__":
    unittest.main()
