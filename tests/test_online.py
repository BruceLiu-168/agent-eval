import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent_eval.online import OnlineStore, redact


AS_OF = "2026-10-04T12:00:00Z"


def make_record(episode_id="e1", success=False, verified=True):
    return {
        "episode": {
            "episode_id": episode_id, "case_id": "c1", "agent_version": "v1",
            "final_state": {"ticket": "open"},
            "events": [{"action": "lookup", "args": {}, "result": {"ok": True}, "agent_id": "support"}],
            "cost": 0.02, "latency_ms": 30, "status": "completed", "metadata": {},
        },
        "observed_at": "2026-10-01T00:00:00Z",
        "outcome": {"mature_at": "2026-10-03T00:00:00Z", "success": success, "source": "ticket_state"},
        "risk_flags": [],
        "review": {"verified": verified, "reviewer": "reviewer-1" if verified else ""},
        "replay_case": {
            "case_id": "c1", "business": "support", "family_id": "close-ticket",
            "input": "Close the resolved ticket", "initial_state": {"ticket": "open"},
            "expected_state": {"ticket": "closed"}, "forbidden_actions": ["delete"],
            "required_actions": ["close"], "required_order": [["lookup", "close"]],
            "limits": {"max_steps": 4, "max_cost": 0.1, "max_latency_ms": 100},
        },
    }


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = OnlineStore(self.root / "online.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_idempotence_conflict_and_persistence(self):
        record = make_record()
        self.assertTrue(self.store.ingest(record, 1)["inserted"])
        retry = self.store.ingest(record, 0)
        self.assertFalse(retry["inserted"])
        self.assertTrue(retry["random_selected"])
        self.assertEqual(retry["sample_rate"], 1)
        conflict = copy.deepcopy(record)
        conflict["episode"]["agent_version"] = "v2"
        with self.assertRaisesRegex(ValueError, "conflicting episode_id"):
            self.store.ingest(conflict)
        with OnlineStore(self.root / "online.sqlite") as second:
            self.assertEqual(second.report(AS_OF)["observed"], 1)

    def test_random_sampling_is_stable_independent_of_risk_and_order(self):
        with OnlineStore(self.root / "second.sqlite") as second:
            choices = {}
            for index in range(100):
                record = make_record(f"e{index}")
                choices[index] = self.store.ingest(record, 0.2)["random_selected"]
            for index in reversed(range(100)):
                record = make_record(f"e{index}")
                record["risk_flags"] = ["high_risk"]
                self.assertEqual(second.ingest(record, 0.2)["random_selected"], choices[index])
            self.assertTrue(0 < sum(choices.values()) < 100)

    def test_maturity_unknown_and_risk_exclusion(self):
        self.store.ingest(make_record("success", True))
        self.store.ingest(make_record("failure", False))
        pending = make_record("pending", True)
        pending["outcome"]["mature_at"] = "2026-10-05T00:00:00Z"
        self.store.ingest(pending)
        self.store.ingest(make_record("unknown", None))
        risk = make_record("risk", False)
        risk["risk_flags"] = ["complaint"]
        self.store.ingest(risk, 0)
        future = make_record("future", False)
        future["observed_at"] = "2026-10-05T00:00:00Z"
        self.store.ingest(future)
        report = self.store.report(AS_OF)
        self.assertEqual(report["random"]["mature_labelled"], 2)
        self.assertEqual(report["random"]["pending"], 1)
        self.assertEqual(report["random"]["unknown"], 1)
        self.assertEqual(report["random"]["unweighted_sample_success_rate"], 0.5)
        # No estimate for the whole population when one intake stratum has p=0.
        self.assertIsNone(report["random"]["success_rate"])
        self.assertEqual(report["risk_discovery"]["failures"], 1)
        self.assertNotIn("success_rate", report["risk_discovery"])
        self.assertEqual(report["future_excluded"], 1)

    def test_random_only_success_rate_and_boundary(self):
        self.store.ingest(make_record("a", True))
        self.store.ingest(make_record("b", False))
        before = self.store.report("2026-10-02T23:59:59Z")
        self.assertIsNone(before["random"]["success_rate"])
        self.assertEqual(before["random"]["pending"], 2)
        exact = self.store.report("2026-10-03T02:00:00+02:00")
        self.assertEqual(exact["random"]["success_rate"], 0.5)
        self.assertEqual(exact["sample_rate"], 1)

    def test_overlap_explicit_and_does_not_duplicate_random_denominator(self):
        record = make_record(success=True)
        record["risk_flags"] = ["flag"]
        self.store.ingest(record)
        report = self.store.report(AS_OF)
        self.assertEqual(report["overlap"], 1)
        self.assertEqual(report["random"]["mature_labelled"], 1)
        self.assertEqual(report["risk_discovery"]["selected"], 1)

    def test_recursive_redaction_in_database_and_promoted_file(self):
        record = make_record()
        record["episode"]["metadata"] = {"access_token": "token-abc", "nested": [{"password": "password-abc"}],
                                             "message": "Contact alice@example.com"}
        record["replay_case"]["input"] = "Contact alice@example.com"
        original = copy.deepcopy(record)
        self.store.ingest(record)
        output = self.root / "regression.jsonl"
        self.store.promote(output, AS_OF)
        self.assertEqual(record, original)
        stored = self.store.connection.execute("SELECT payload, intake_payload FROM episodes").fetchone()
        combined = str(tuple(stored)) + output.read_text()
        for secret in ("token-abc", "password-abc", "alice@example.com"):
            self.assertNotIn(secret, combined)
        self.assertIn("[REDACTED]", combined)
        self.assertEqual(redact({"Authorization": "Bearer top-secret"}), {"Authorization": "[REDACTED]"})

    def test_promotion_requires_review_mature_failure_and_oracle(self):
        self.store.ingest(make_record("valid"))
        self.store.ingest(make_record("successful", True))
        self.store.ingest(make_record("unknown", None))
        self.store.ingest(make_record("unreviewed", False, False))
        pending = make_record("pending")
        pending["outcome"]["mature_at"] = "2026-10-10T00:00:00Z"
        self.store.ingest(pending)
        missing = make_record("missing-oracle")
        del missing["replay_case"]["expected_state"]
        self.store.ingest(missing)
        empty = make_record("empty-oracle")
        empty["replay_case"]["expected_state"] = {}
        self.store.ingest(empty)
        output = self.root / "regression.jsonl"
        result = self.store.promote(output, AS_OF)
        self.assertEqual(result["promoted"], 1)
        self.assertEqual(len(result["rejected"]), 2)
        self.assertTrue(all("oracle" in item["reason"] for item in result["rejected"]))
        case = json.loads(output.read_text())
        self.assertEqual(case["expected_state"], {"ticket": "closed"})
        self.assertNotEqual(case["expected_state"], make_record()["episode"]["final_state"])

    def test_email_pseudonyms_preserve_authorization_identity_and_keys(self):
        record = make_record()
        state = {"actor": "alice@example.com", "owner": "bob@example.com", "service": "token@example.com",
                 "identities": {"alice@example.com": "user", "bob@example.com": "owner", "token@example.com": "service"}}
        record["episode"]["final_state"] = copy.deepcopy(state)
        record["replay_case"]["initial_state"] = copy.deepcopy(state)
        record["replay_case"]["expected_state"] = {"owner": "bob@example.com", "authorized": False}
        self.store.ingest(record)
        output = self.root / "regression.jsonl"
        self.store.promote(output, AS_OF)
        promoted = json.loads(output.read_text())
        safe = promoted["initial_state"]
        self.assertNotEqual(safe["actor"], safe["owner"])
        self.assertEqual(safe["owner"], promoted["expected_state"]["owner"])
        self.assertEqual(len(safe["identities"]), 3)
        self.assertEqual(safe["identities"][safe["actor"]], "user")
        self.assertEqual(safe["identities"][safe["owner"]], "owner")
        self.assertEqual(safe["identities"][safe["service"]], "service")
        with OnlineStore(self.root / "online.sqlite") as reopened:
            self.assertFalse(reopened.ingest(record)["inserted"])
        # Plain source emails must not occur anywhere in the SQLite file or export.
        content = (self.root / "online.sqlite").read_bytes() + output.read_bytes()
        self.assertNotIn(b"alice@example.com", content)
        self.assertNotIn(b"bob@example.com", content)
        self.assertNotIn(b"token@example.com", content)
        with OnlineStore(self.root / "independent.sqlite") as independent:
            independent.ingest(record)
            payload = json.loads(independent.connection.execute("SELECT payload FROM episodes").fetchone()[0])
            self.assertNotEqual(payload["episode"]["final_state"]["actor"], safe["actor"])

    def test_redaction_refuses_crafted_literal_pseudonym_collision(self):
        pseudonym = redact("alice@example.com")
        with self.assertRaisesRegex(ValueError, "reserved"):
            redact({"alice@example.com": "first", pseudonym: "second"})
        self.assertNotEqual(redact("alice@example.com"), redact("bob@example.com"))

    def test_promotion_deduplicates_sources_semantic_cases_and_existing_file(self):
        self.store.ingest(make_record("e1"))
        renamed = make_record("e2")
        renamed["replay_case"]["case_id"] = "renamed-copy"
        self.store.ingest(renamed)
        output = self.root / "regression.jsonl"
        result = self.store.promote(output, AS_OF)
        self.assertEqual(result["promoted"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.store.promote(output, AS_OF)["promoted"], 0)
        self.assertEqual(len(output.read_text().splitlines()), 1)
        with OnlineStore(self.root / "fresh.sqlite") as fresh:
            fresh.ingest(make_record("new-source"))
            self.assertEqual(fresh.promote(output, AS_OF)["duplicates"], 1)
        distinct = make_record("e3")
        distinct["replay_case"]["case_id"] = "c2"
        distinct["replay_case"]["initial_state"] = {"ticket": "paused"}
        self.store.ingest(distinct)
        self.assertEqual(self.store.promote(output, AS_OF)["promoted"], 1)
        self.assertEqual(len(output.read_text().splitlines()), 2)

    def test_explicit_delayed_label_and_review_preserve_sampling(self):
        record = make_record(success=None, verified=False)
        original = self.store.ingest(record)
        self.assertEqual(self.store.report(AS_OF)["random"]["unknown"], 1)
        self.store.annotate("e1", outcome={"mature_at": "2026-10-03T00:00:00Z", "success": False,
                                          "source": "business-db"},
                            review={"verified": True, "reviewer": "human"})
        self.assertEqual(self.store.report(AS_OF)["random"]["failures"], 1)
        retry = self.store.ingest(record, 0)
        self.assertFalse(retry["inserted"])
        self.assertEqual(retry["random_selected"], original["random_selected"])
        self.assertEqual(self.store.promote(self.root / "regression.jsonl", AS_OF)["promoted"], 1)
        with self.assertRaises(KeyError):
            self.store.annotate("missing", review={"verified": True, "reviewer": "human"})

    def test_promotion_restores_deleted_file_or_missing_case(self):
        self.store.ingest(make_record("e1"))
        second = make_record("e2")
        second["replay_case"]["case_id"] = "c2"
        second["replay_case"]["initial_state"] = {"ticket": "paused"}
        self.store.ingest(second)
        output = self.root / "regression.jsonl"
        self.assertEqual(self.store.promote(output, AS_OF)["promoted"], 2)
        original = output.read_bytes()
        output.unlink()
        self.assertEqual(self.store.promote(output, AS_OF)["promoted"], 2)
        self.assertEqual(output.read_bytes(), original)
        output.write_text(output.read_text().splitlines()[0] + "\n")
        self.assertEqual(self.store.promote(output, AS_OF)["promoted"], 1)
        self.assertEqual(output.read_bytes(), original)

    def test_promotion_refuses_conflicting_case_id_without_corrupting_dataset(self):
        self.store.ingest(make_record("original"))
        output = self.root / "regression.jsonl"
        self.store.promote(output, AS_OF)
        before = output.read_bytes()
        conflict = make_record("conflicting-source")
        conflict["replay_case"]["initial_state"] = {"ticket": "paused"}
        self.store.ingest(conflict)
        result = self.store.promote(output, AS_OF)
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("case_id conflict", result["rejected"][0]["reason"])
        self.assertEqual(output.read_bytes(), before)

    def test_required_order_requires_pairs_compatible_with_offline(self):
        invalid = make_record("invalid-order")
        invalid["replay_case"]["required_order"] = [["lookup", "verify", "close"]]
        self.store.ingest(invalid)
        output = self.root / "regression.jsonl"
        result = self.store.promote(output, AS_OF)
        self.assertEqual(result["promoted"], 0)
        self.assertIn("action pairs", result["rejected"][0]["reason"])
        self.assertFalse(output.exists())

    def test_success_before_pairs_are_validated_preserved_and_fingerprinted(self):
        ordinary = make_record("ordinary")
        guarded = make_record("guarded")
        guarded["replay_case"]["case_id"] = "c2"
        guarded["replay_case"]["required_success_before"] = [["verify", "close"]]
        self.store.ingest(ordinary)
        self.store.ingest(guarded)
        output = self.root / "regression.jsonl"
        self.assertEqual(self.store.promote(output, AS_OF)["promoted"], 2)
        cases = {case["case_id"]: case for case in map(json.loads, output.read_text().splitlines())}
        self.assertEqual(cases["c2"]["required_success_before"], [["verify", "close"]])
        for index, bad_order in enumerate((["verify", "close"], [["verify"]], [["verify", 2]], None)):
            invalid = make_record(f"invalid-{index}")
            invalid["replay_case"]["required_success_before"] = bad_order
            self.store.ingest(invalid)
        result = self.store.promote(output, AS_OF)
        self.assertEqual(len(result["rejected"]), 4)
        self.assertTrue(all("required_success_before" in item["reason"] for item in result["rejected"]))

    def test_naive_timestamps_and_invalid_values_rejected(self):
        for key in ("observed_at", "mature_at"):
            record = make_record()
            target = record if key == "observed_at" else record["outcome"]
            target[key] = "2026-10-01T00:00:00"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "naive"):
                self.store.ingest(record)
        with self.assertRaisesRegex(ValueError, "naive"):
            self.store.report("2026-10-04")
        for rate in (-1, 1.1, float("nan"), True):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.store.ingest(make_record(), rate)
        record = make_record()
        record["outcome"]["success"] = 1
        with self.assertRaises(ValueError):
            self.store.ingest(record)
        record = make_record()
        record["review"]["reviewer"] = ""
        with self.assertRaises(ValueError):
            self.store.ingest(record)

    def test_invalid_existing_regression_file_is_unchanged(self):
        self.store.ingest(make_record())
        output = self.root / "regression.jsonl"
        output.write_text('{"case_id":"broken"}\n')
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "invalid regression"):
            self.store.promote(output, AS_OF)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM promotions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
