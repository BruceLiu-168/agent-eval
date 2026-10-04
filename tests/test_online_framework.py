import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_eval.contracts import load_dataset
from agent_eval.grading import evaluate
from agent_eval.online import OnlineStore, _case_error, _fingerprint, main


AS_OF = "2026-10-04T12:00:00Z"


def record(identifier="e1", expected="Correct answer", success=False, verified=True):
    return {
        "episode": {"episode_id": identifier, "case_id": "c1", "agent_version": "v1",
                    "status": "completed", "events": [{"action": "answer"}],
                    "output": "Wrong answer", "cost": None, "latency_ms": None},
        "observed_at": "2026-10-01T00:00:00Z",
        "outcome": {"mature_at": "2026-10-03T00:00:00Z", "success": success, "source": "business-audit"},
        "review": {"verified": verified, "reviewer": "human" if verified else ""},
        "replay_case": {"case_id": "c1", "business": "qa", "input": "A question", "expected_output": expected},
    }


class OnlineFrameworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "online.sqlite"
        self.store = OnlineStore(self.db)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_answer_only_failure_promotes_and_is_ready_for_framework_replay(self):
        self.store.ingest(record())
        destination = self.root / "regression.jsonl"
        result = self.store.promote(destination, AS_OF)
        self.assertEqual(result["promoted"], 1)
        case = load_dataset(destination)[0]
        self.assertEqual(case["family_id"], "c1")
        self.assertNotIn("expected_state", case)
        self.assertNotIn("initial_state", case)
        observed = record()["episode"]
        self.assertEqual(evaluate(case, observed)["status"], "fail")
        observed["output"] = "Correct answer"
        self.assertEqual(evaluate(case, observed)["status"], "pass")

    def test_structured_output_and_explicit_null_are_valid_replay_oracles(self):
        for index, expected in enumerate(({"answer": "yes", "citations": ["doc-1"]}, None)):
            item = record(f"e{index}", expected=expected)
            item["episode"]["case_id"] = item["replay_case"]["case_id"] = f"case-{index}"
            self.store.ingest(item)
        destination = self.root / "structured.jsonl"
        self.assertEqual(self.store.promote(destination, AS_OF)["promoted"], 2)
        cases = {case["case_id"]: case for case in load_dataset(destination)}
        self.assertIsNone(cases["case-1"]["expected_output"])
        self.assertEqual(cases["case-0"]["expected_output"]["citations"], ["doc-1"])

    def test_missing_oracle_is_rejected_without_guessing_from_failed_output(self):
        item = record()
        del item["replay_case"]["expected_output"]
        self.store.ingest(item)
        destination = self.root / "missing.jsonl"
        result = self.store.promote(destination, AS_OF)
        self.assertEqual(result["promoted"], 0)
        self.assertIn("missing oracle", result["rejected"][0]["reason"])
        self.assertFalse(destination.exists())

    def test_checks_and_rubric_are_preserved_and_affect_semantic_fingerprint(self):
        first = record("rules")
        del first["replay_case"]["expected_output"]
        first["replay_case"]["checks"] = [{"id": "answer", "kind": "contains", "path": "output", "expected": "citation"}]
        first["replay_case"]["rubric"] = [{"id": "helpful", "description": "Answer is useful"}]
        second = copy.deepcopy(first)
        second["episode"]["episode_id"] = "different-rubric"
        second["episode"]["case_id"] = second["replay_case"]["case_id"] = "c2"
        second["replay_case"]["rubric"][0]["description"] = "Answer is grounded in observed evidence"
        self.assertNotEqual(_fingerprint(first["replay_case"]), _fingerprint(second["replay_case"]))
        third = copy.deepcopy(first["replay_case"])
        third["checks"][0]["expected"] = "different citation"
        self.assertNotEqual(_fingerprint(first["replay_case"]), _fingerprint(third))
        self.store.ingest(first)
        self.store.ingest(second)
        destination = self.root / "rules.jsonl"
        self.assertEqual(self.store.promote(destination, AS_OF)["promoted"], 2)
        self.assertTrue(all(case["checks"] and case["rubric"] for case in load_dataset(destination)))

    def test_renamed_answer_only_copy_deduplicates_after_normalization(self):
        first, second = record("first"), record("second")
        second["replay_case"]["case_id"] = "renamed"
        self.store.ingest(first)
        self.store.ingest(second)
        destination = self.root / "deduplicated.jsonl"
        result = self.store.promote(destination, AS_OF)
        self.assertEqual((result["promoted"], result["duplicates"]), (1, 1))
        self.assertEqual(self.store.promote(destination, AS_OF)["promoted"], 0)
        self.assertEqual(len(load_dataset(destination)), 1)

    def test_output_oracle_changes_are_not_deduplicated_and_metadata_is(self):
        first = record()["replay_case"]
        changed = {**first, "expected_output": "Other answer"}
        self.assertNotEqual(_fingerprint(first), _fingerprint(changed))
        self.assertEqual(_fingerprint(first), _fingerprint({**first, "metadata": {"source": "production"}}))

    def test_unified_contract_rejects_invalid_rules_and_nonfinite_business_values(self):
        self.assertIn("replay_case.checks", _case_error({**record()["replay_case"], "checks": [
            {"id": "bad", "kind": "regex", "path": "output", "expected": "["}]}))
        item = record()
        item["episode"]["output"] = {"number": float("nan")}
        with self.assertRaisesRegex(ValueError, "episode.output.number"):
            self.store.ingest(item)

    def test_cli_annotate_backfills_delayed_label_and_review_without_resampling(self):
        item = record(success=None, verified=False)
        original = self.store.ingest(item)
        self.assertEqual(self.store.report(AS_OF)["random"]["unknown"], 1)
        outcome = self.root / "outcome.json"
        review = self.root / "review.json"
        outcome.write_text(json.dumps({"mature_at": "2026-10-03T00:00:00Z", "success": False, "source": "business-db"}))
        review.write_text(json.dumps({"verified": True, "reviewer": "human"}))
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--db", str(self.db), "annotate", "--episode-id", "e1",
                         "--outcome", str(outcome), "--review", str(review)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), {"episode_id": "e1", "updated": True})
        self.assertEqual(self.store.report(AS_OF)["random"]["failures"], 1)
        retry = self.store.ingest(item, random_rate=0)
        self.assertEqual(retry["random_selected"], original["random_selected"])
        self.assertEqual(self.store.promote(self.root / "annotated.jsonl", AS_OF)["promoted"], 1)

    def test_cli_annotate_accepts_each_file_independently(self):
        self.store.ingest(record(success=None, verified=False))
        for option, value in (("outcome", {"mature_at": "2026-10-03T00:00:00Z", "success": False, "source": "db"}),
                              ("review", {"verified": True, "reviewer": "human"})):
            source = self.root / (option + ".json")
            source.write_text(json.dumps(value))
            with self.subTest(option=option), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", str(self.db), "annotate", "--episode-id", "e1",
                                       "--" + option, str(source)]), 0)
        self.assertEqual(self.store.promote(self.root / "one-by-one.jsonl", AS_OF)["promoted"], 1)

    def test_cli_annotate_requires_an_update_and_sanitizes_errors(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            main(["--db", str(self.db), "annotate", "--episode-id", "e1"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("at least one", stderr.getvalue())
        source = self.root / "secret-api-key.json"
        source.write_text('{"verified":true,"reviewer":"human"}')
        for args in (["--episode-id", "secret-api-key", "--review", str(source)],
                     ["--episode-id", "e1", "--review", str(self.root / "secret-api-key-missing.json")]):
            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                main(["--db", str(self.db), "annotate", *args])
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("secret-api-key", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
