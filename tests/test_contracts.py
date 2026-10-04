import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent_eval.contracts import load_dataset, public_case, validate_case, validate_episode


def case(**updates):
    return {"case_id": "case-1", "business": "support", "input": {"query": "hello"},
            "expected_output": "hello", **updates}


def episode(**updates):
    return {"episode_id": "episode-1", "case_id": "case-1", "agent_version": "v1",
            "status": "completed", "events": [], "output": "hello", **updates}


class ContractTests(unittest.TestCase):
    def test_case_normalizes_and_preserves_private_metadata_without_aliasing(self):
        raw = case(metadata={"labels": ["secret"]})
        result = validate_case(raw)
        self.assertEqual(result["family_id"], "case-1")
        result["metadata"]["labels"].append("changed")
        self.assertEqual(raw["metadata"]["labels"], ["secret"])
        self.assertNotIn("family_id", raw)

    def test_agent_boundary_is_a_whitelist(self):
        private = case(expected_state={"secret": 42}, metadata={"reference": "hidden"},
                       rubric=[{"id": "one", "description": "secret rule"}],
                       future_oracle="also secret", initial_state={"open": True}, limits={"max_steps": 3})
        public = public_case(private)
        self.assertEqual(set(public), {"case_id", "business", "input", "initial_state", "limits"})
        public["input"]["query"] = "changed"
        self.assertEqual(private["input"]["query"], "hello")

    def test_explicit_null_is_an_oracle(self):
        self.assertIn("expected_output", validate_case(case(expected_output=None)))

    def test_missing_oracle_is_rejected(self):
        value = case()
        del value["expected_output"]
        with self.assertRaisesRegex(ValueError, "requires expected_state"):
            validate_case(value)

    def test_business_numbers_can_be_negative_but_must_be_finite(self):
        self.assertEqual(validate_case(case(input=-3, expected_output=-2))["input"], -3)
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "case.input.deep"):
                validate_case(case(input={"deep": value}))

    def test_invalid_case_fields_have_field_paths(self):
        mutations = [({"case_id": ""}, "case.case_id"), ({"input": {1: "x"}}, "case.input"),
                     ({"expected_state": {}}, "case.expected_state"), ({"metadata": []}, "case.metadata"),
                     ({"limits": {"max_cost": -1}}, "case.limits.max_cost"),
                     ({"limits": {"max_cost": True}}, "case.limits.max_cost"),
                     ({"limits": {"max_steps": 1.2}}, "case.limits.max_steps"),
                     ({"limits": {"max_token": 5}}, "case.limits.max_token"),
                     ({"required_actions": [False]}, "case.required_actions"),
                     ({"required_order": [["a"]]}, "case.required_order"),
                     ({"initial_state": None}, "case.initial_state")]
        for update, path in mutations:
            with self.subTest(update=update), self.assertRaises(ValueError) as caught:
                validate_case(case(**update))
            self.assertIn(path, str(caught.exception))

    def test_declarative_checks_reject_invalid_patterns_kinds_and_paths(self):
        base = {"id": "one", "kind": "equals", "path": "output.answer", "expected": 1}
        for updates in ({"kind": "bad"}, {"kind": []}, {"path": "metadata.answer"},
                        {"path": "output..answer"}, {"kind": "regex", "expected": "["},
                        {"kind": "regex", "expected": 1}, {"kind": "json_subset", "expected": []}):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "case.checks"):
                validate_case(case(checks=[{**base, **updates}]))
        with self.assertRaisesRegex(ValueError, "duplicate ID"):
            validate_case(case(checks=[base, base]))

    def test_cycles_and_non_json_values_are_rejected(self):
        value = case()
        value["metadata"] = value
        with self.assertRaisesRegex(ValueError, "cyclic"):
            validate_case(value)
        with self.assertRaisesRegex(ValueError, "JSON value"):
            validate_case(case(input=(1, 2)))

    def test_episode_identity_and_version_are_enforced(self):
        for kwargs, field in (({"case_id": "other"}, "case_id"), ({"version": "v2"}, "agent_version")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, field):
                validate_episode(episode(), **kwargs)

    def test_metrics_allow_absent_null_zero_but_reject_invalid_numbers(self):
        self.assertNotIn("cost", validate_episode(episode()))
        self.assertIsNone(validate_episode(episode(cost=None))["cost"])
        self.assertEqual(validate_episode(episode(latency_ms=0))["latency_ms"], 0)
        for value in (-1, True, float("inf"), 10 ** 400):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "episode.cost"):
                validate_episode(episode(cost=value))
        with self.assertRaisesRegex(ValueError, "episode.events"):
            validate_episode(episode(events=[{"action": "tool", "latency_ms": -1}]))

    def test_output_absence_is_distinct_from_null(self):
        raw = episode(output=None)
        self.assertIsNone(validate_episode(raw)["output"])
        del raw["output"]
        self.assertNotIn("output", validate_episode(raw))

    def test_required_episode_fields_and_events(self):
        for field in ("episode_id", "case_id", "agent_version", "events", "status"):
            raw = episode()
            del raw[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "episode." + field):
                validate_episode(raw)
        for events in ([{"action": ""}], [{}], ["tool"], {}):
            with self.subTest(events=events), self.assertRaisesRegex(ValueError, "episode.events"):
                validate_episode(episode(events=events))

    def test_jsonl_locations_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(json.dumps(case()) + "\n\n" + json.dumps(case()), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cases.jsonl:3:.*duplicate case ID"):
                load_dataset(path)
            path.write_text("\n" + json.dumps(case(input=float("nan"))), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cases.jsonl:2:.*case.input"):
                load_dataset(path)
            path.write_text('{"case_id":"a","case_id":"b"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                load_dataset(path)
            path.write_text(" \n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                load_dataset(path)
            path.write_text("\n" + json.dumps(case()) + "\n", encoding="utf-8")
            self.assertEqual(load_dataset(path)[0]["family_id"], "case-1")

    def test_existing_demo_dataset_is_compatible(self):
        source = Path(__file__).resolve().parents[1] / "examples" / "cases.jsonl"
        self.assertEqual(len(load_dataset(source)), 13)


if __name__ == "__main__":
    unittest.main()
